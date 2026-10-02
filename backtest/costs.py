"""Ce qu'une opération coûte vraiment : frais, glissement, pont, gaz.

Un backtest sans coûts dit seulement que la stratégie marche dans un monde où
elle est gratuite. Les re-centrages sont la moitié du montage et chacun paie un
échange, un pont et deux ordres : au rythme de plusieurs par mois, un chiffre
supposé à 5 points de base au lieu de 30 déplace le résultat annuel plus que la
cible de LTV qu'on cherche à arbitrer.

**Chaque paramètre porte sa provenance et dit s'il a été VÉRIFIÉ.** C'est la
règle 7 du CLAUDE.md du dépôt, appliquée ici parce que c'est ici qu'elle se
perdrait : un taux de frais recopié de mémoire dans un coin de module devient
indiscernable d'un taux lu sur la place. `unverified()` rend la liste de ce qui
reste supposé, pour qu'un rapport puisse la publier au lieu de la taire, et pour
que le gel de la méthode sache exactement quoi aller lire.

Deux distinctions qui ont l'air d'ergoter et qui ne le sont pas :

- **le plafond n'est pas le coût attendu.** `slippage_max_bps` du `config.yaml`
  est une limite que le bot REFUSE de dépasser ; ce module décrit ce qu'une
  opération coûte quand elle passe. Prendre le plafond pour l'espérance
  surestimerait le coût d'un facteur 3 à 6 et ferait rejeter des cibles saines.
- **le wstETH se vend au prix du MARCHÉ, pas à celui de l'oracle.** L'oracle
  Aave applique le taux de conversion au prix ETH/USD ; un vendeur, lui,
  encaisse ce que le carnet stETH/ETH veut bien donner. C'est exactement le décrochage de juin 2022
  (plus bas 0,935), qui ne liquide pas mais coûte à la SORTIE — d'où un
  glissement d'échange qui se lit sur `backtest/steth.py` et non sur l'oracle.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from delta0.types import ActionKind

BPS = 1e-4
GWEI = 1e-9


@dataclass(frozen=True, slots=True)
class Param:
    """Un nombre, d'où il vient, et s'il a été lu ou supposé.

    `verified` ne veut pas dire « raisonnable » : il veut dire que quelqu'un est
    allé le lire sur la place ou l'a mesuré, et que `source` dit où et quand.
    """

    value: float
    unit: str
    source: str
    verified: bool = False

    def __str__(self) -> str:
        mark = "" if self.verified else "  NON VÉRIFIÉ"
        return f"{self.value:g} {self.unit} — {self.source}{mark}"


@dataclass(frozen=True, slots=True)
class Costs:
    """Le barème complet. Aucun défaut n'est neutre : chacun s'assume par écrit."""

    swap_fee: Param
    swap_slippage: Param
    hl_taker_fee: Param
    hl_maker_fee: Param
    bridge_fee: Param
    bridge_fixed: Param
    aave_gas: Param
    swap_gas: Param
    bridge_gas: Param
    gas_price: Param

    def params(self) -> dict[str, Param]:
        return {
            name: getattr(self, name)
            for name in (
                "swap_fee",
                "swap_slippage",
                "hl_taker_fee",
                "hl_maker_fee",
                "bridge_fee",
                "bridge_fixed",
                "aave_gas",
                "swap_gas",
                "bridge_gas",
                "gas_price",
            )
        }

    def unverified(self) -> dict[str, Param]:
        """Ce qui reste supposé. Un rapport qui ne publie pas cette liste ment par omission."""
        return {name: param for name, param in self.params().items() if not param.verified}

    def with_param(self, name: str, param: Param) -> Costs:
        """Un barème où un paramètre a été remplacé — par une lecture, ou pour un balayage."""
        if name not in self.params():
            raise KeyError(f"paramètre de coût inconnu : {name}")
        return replace(self, **{name: param})


# Le barème par défaut. Tout ce qui n'est pas marqué vérifié est à lire avant que
# le moteur rende un chiffre retenu ; la revue finance a la liste par
# `DEFAULT.unverified()`.
DEFAULT = Costs(
    swap_fee=Param(
        5.0,
        "bps",
        "pool Uniswap v3 wstETH/WETH sur Arbitrum, palier suppose 0,05 %"
        " — a lire sur le pool reellement route au moment du gel",
    ),
    swap_slippage=Param(
        5.0,
        "bps",
        "impact de marche suppose a la taille du montage (~40 k$ sur une paire profonde)"
        " — a mesurer par un devis de routeur, pas a deduire du plafond du config.yaml",
    ),
    hl_taker_fee=Param(
        4.5,
        "bps",
        "bareme public Hyperliquid, palier de base, de memoire"
        " — a lire par l'API (type userFees) avec l'adresse maitre",
    ),
    hl_maker_fee=Param(
        1.5,
        "bps",
        "bareme public Hyperliquid, palier de base, de memoire"
        " — a lire par l'API (type userFees) avec l'adresse maitre",
    ),
    bridge_fee=Param(
        4.0,
        "bps",
        "pont Arbitrum <-> Hyperliquid, part proportionnelle supposee"
        " — le run M1 a mesure le DELAI (p95 316 s), jamais le prix",
    ),
    bridge_fixed=Param(
        1.0,
        "usd",
        "part fixe supposee du pont — meme remarque : le M1 a chronometre, pas facture",
    ),
    aave_gas=Param(
        932_000,
        "gas",
        "cycle Aave complet mesure sur fork anvil le 2026-09-08 (memory/aave_findings.md)",
        verified=True,
    ),
    swap_gas=Param(
        250_000,
        "gas",
        "echange sur routeur, ordre de grandeur suppose — a mesurer sur fork comme le cycle Aave",
    ),
    bridge_gas=Param(
        150_000,
        "gas",
        "depot vers le pont, ordre de grandeur suppose — a mesurer sur fork",
    ),
    gas_price=Param(
        0.01,
        "gwei",
        "Arbitrum au repos, ordre de grandeur suppose"
        " — a relire sur la periode, un pic de congestion ne se devine pas",
    ),
)


@dataclass(frozen=True, slots=True)
class Charge:
    """Ce qu'une opération a coûté, par nature. Les natures ne se mélangent pas.

    Les additionner tout de suite ferait perdre la seule chose qu'un rapport
    doit pouvoir dire : si le coût annuel vient des frais (donc du rythme de
    re-centrage, réglable) ou du glissement (donc de la taille, qui ne l'est
    pas).
    """

    fee_usd: float = 0.0
    slippage_usd: float = 0.0
    bridge_usd: float = 0.0
    gas_usd: float = 0.0

    @property
    def total_usd(self) -> float:
        return self.fee_usd + self.slippage_usd + self.bridge_usd + self.gas_usd

    def __add__(self, other: Charge) -> Charge:
        return Charge(
            fee_usd=self.fee_usd + other.fee_usd,
            slippage_usd=self.slippage_usd + other.slippage_usd,
            bridge_usd=self.bridge_usd + other.bridge_usd,
            gas_usd=self.gas_usd + other.gas_usd,
        )


@dataclass(frozen=True, slots=True)
class Legs:
    """Ce qu'une action fait physiquement, d'où découle ce qu'elle paie.

    La table plus bas est la seule description de ces enchaînements dans le
    backtest. Elle vient du README §8 : c'est là que se lit, par exemple, qu'une
    pompe descendante (P6) traverse le pont alors qu'un ajout de marge (P2) est
    local — la différence entre 316 secondes et une seconde, et entre un coût de
    pont et rien du tout.
    """

    arbitrum_txs: int = 0
    bridged: bool = False
    swapped: bool = False
    hl_order: bool = False


# README §8. Une action absente de cette table ne coûte rien et doit le mériter.
LEGS: dict[ActionKind, Legs] = {
    "NOOP": Legs(),
    # P2 : un seul appel local à Hyperliquid. Ni pont, ni chaîne, ni ordre —
    # c'est ce qui en fait la seule défense rapide du flanc haut (hl_findings §10).
    "ADD_ISOLATED_MARGIN": Legs(),
    "REDUCE": Legs(hl_order=True),
    "REPAY_FROM_CUSHION": Legs(arbitrum_txs=1),
    # P4 : retirer du collatéral, le vendre, rembourser. L'échange est ce qui
    # rend ce chemin cher, et c'est là que le prix de MARCHÉ du stETH mord.
    "STEPWISE_DELEVERAGE": Legs(arbitrum_txs=2, swapped=True),
    "PUMP_UP": Legs(arbitrum_txs=1, bridged=True),
    "PUMP_DOWN": Legs(arbitrum_txs=1, bridged=True),
    "RECENTER_UP": Legs(arbitrum_txs=2, bridged=True, swapped=True, hl_order=True),
    "RECENTER_DOWN": Legs(arbitrum_txs=2, bridged=True, swapped=True, hl_order=True),
    "RETRUE_SHORT": Legs(hl_order=True),
    "SKIM_RECOMPOSE": Legs(arbitrum_txs=2, bridged=True, swapped=True, hl_order=True),
    "REGIME_STEP": Legs(arbitrum_txs=2, bridged=True, swapped=True, hl_order=True),
    "LIQUIDATION_RESPONSE": Legs(hl_order=True),
}


def gas_usd(units: float, costs: Costs, eth_price: float) -> float:
    return units * costs.gas_price.value * GWEI * eth_price


def swap_charge(amount_usd: float, costs: Costs, eth_price: float) -> Charge:
    """Vendre ou acheter du wstETH : frais de pool, impact, et le gaz de l'échange.

    Le décrochage du stETH n'est PAS ici : ce n'est pas un coût de transaction
    mais un prix d'exécution, et il se lit sur la série de marché au moment de
    la vente. Le confondre avec un glissement le ferait payer deux fois.
    """
    if amount_usd <= 0.0:
        return Charge()
    return Charge(
        fee_usd=amount_usd * costs.swap_fee.value * BPS,
        slippage_usd=amount_usd * costs.swap_slippage.value * BPS,
        gas_usd=gas_usd(costs.swap_gas.value, costs, eth_price),
    )


def order_charge(notional_usd: float, costs: Costs, *, maker: bool) -> Charge:
    """Un ordre sur Hyperliquid. `maker` est un espoir, pas une garantie.

    README §9.1 poste en maker puis traverse : un backtest qui suppose le maker
    partout sous-estime le coût des urgences, qui sont précisément celles qui
    traversent. Les urgences passent donc `maker=False`.
    """
    if notional_usd <= 0.0:
        return Charge()
    rate = costs.hl_maker_fee.value if maker else costs.hl_taker_fee.value
    return Charge(fee_usd=notional_usd * rate * BPS)


def bridge_charge(amount_usd: float, costs: Costs, eth_price: float) -> Charge:
    if amount_usd <= 0.0:
        return Charge()
    return Charge(
        bridge_usd=amount_usd * costs.bridge_fee.value * BPS + costs.bridge_fixed.value,
        gas_usd=gas_usd(costs.bridge_gas.value, costs, eth_price),
    )


def chain_charge(txs: int, costs: Costs, eth_price: float) -> Charge:
    if txs <= 0:
        return Charge()
    return Charge(gas_usd=txs * gas_usd(costs.aave_gas.value, costs, eth_price))


def charge(
    kind: ActionKind,
    costs: Costs,
    *,
    eth_price: float,
    swapped_usd: float = 0.0,
    bridged_usd: float = 0.0,
    order_usd: float = 0.0,
    maker: bool = True,
) -> Charge:
    """Le coût complet d'une action, nature par nature.

    Les montants sont passés par l'appelant plutôt que déduits ici : le grand
    livre est le seul à savoir combien une action a vraiment déplacé, et une
    action bornée par un solde ne coûte que ce qu'elle a bougé.
    """
    legs = LEGS.get(kind)
    if legs is None:
        raise KeyError(f"action sans description physique : {kind}")
    total = chain_charge(legs.arbitrum_txs, costs, eth_price)
    if legs.swapped:
        total = total + swap_charge(swapped_usd, costs, eth_price)
    if legs.bridged:
        total = total + bridge_charge(bridged_usd, costs, eth_price)
    if legs.hl_order:
        total = total + order_charge(order_usd, costs, maker=maker)
    return total
