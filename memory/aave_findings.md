# Aave v3 (Arbitrum) — comportements vérifiés sur fork

Source : `scripts/precheck_aave_fork.py`, exécuté contre un anvil forkant
Arbitrum One sur le vrai contrat Pool `0x794a61358D6845594F94dc1DB02A252b5b4814aD`.
Zéro dollar dépensé, ABI confirmée à 100 %.

Ces découvertes contraignent le code : `executor.repay_all()` et la séquence de
`tracer._fire_aave_cycle()` en découlent directement. Ne pas les "simplifier"
sans re-passer le precheck.

## 1. `repay` sur une dette nulle revert

Appeler `Pool.repay(asset, amount, rateMode, onBehalfOf)` alors que la dette du
`rateMode` visé est à zéro revert avec `NO_DEBT_OF_SELECTED_TYPE`.

Conséquence : un cycle traceur ne peut pas se contenter de
`approve → supply → repay → withdraw`. Il faut un `borrow` intercalaire pour
qu'il existe une dette à rembourser. C'est pour ça que
`_fire_aave_cycle` emprunte ~20 % du montant supplyé.

## 2. Un repay partiel bloque le withdraw complet

Rembourser exactement le montant emprunté laisse les intérêts courus entre le
bloc du `borrow` et celui du `repay` (fractions de cent, mais non nuls). Le
`withdraw` du collatéral intégral échoue alors : il resterait de la dette non
couverte.

Solution retenue : passer `MAX_UINT256` (`2**256 - 1`) comme `amount` au
`repay`. Aave interprète ce sentinelle comme "ferme toute la position du
`rateMode`", intérêts compris. Il faut approuver un petit buffer au-dessus du
montant emprunté (le code prend `borrow + 1 USDC`) pour couvrir ces intérêts.

Implémentation : `AaveTraceExecutor.repay_all(asset)`.
- Le garde-fou de sécurité (`MicroOpsGuard`) voit un notionnel sur-estimé
  (2 USD) plutôt que la valeur fictive `MAX_UINT256`, sinon le plafond
  `max_op_usd` refuserait systématiquement l'opération.
- Le montant journalisé dans `intents` est `0`, pour signaler explicitement
  qu'il s'agit du sentinelle et pas d'un montant réel.

## 3. Séquence validée (6 transactions, toutes status=1)

```
approve(USDC, montant)                  -> Pool autorisé à tirer le collatéral
supply(USDC, montant, self, 0)
borrow(USDC, 20 % du montant, mode=2, 0, self)
approve(USDC, borrow + 1)               -> buffer pour les intérêts
repay(USDC, MAX_UINT256, mode=2, self)
withdraw(USDC, MAX_UINT256, self)   -> voir 2 bis : le montant exact revert
```

Invariant vérifié en fin de séquence : `solde USDC final == solde USDC initial`
(aux frais de gas près, payés en ETH). C'est le test qui prouve que le cycle
traceur est bien un aller-retour neutre.

`rateMode = 2` = taux variable. Le taux stable est déprécié sur Aave v3.

## 2 bis. Un withdraw du montant exact revert par arrondi

Découvert le 2026-09-02, sur la même séquence qui passait le 2026-08-28 : le
`withdraw(USDC, montant_supplyé, self)` a reverté (`0x47bc4b2c`,
`NotEnoughAvailableUserBalance`) alors que la dette était à zéro.

Cause : Aave stocke un dépôt en parts,
`scaledBalance = montant / liquidityIndex`, et arrondit **vers le bas** à la
division comme à la multiplication de retour. Un supply de 5,000000 USDC peut
donc se relire en 4,999999. Demander le montant exact revient à demander plus
que ce qu'on possède.

Le piège est **intermittent** : que la double troncature perde une unité ou
non dépend de la valeur de l'index au moment du dépôt. C'est pour ça que la
séquence est passée le 28 août et a échoué le 2 septembre, à code identique.
Ne jamais conclure d'un seul passage vert que le montant exact est sûr.

Solution : `withdraw(MAX_UINT256)` — même sentinelle que le repay, Aave le lit
comme « tout mon solde ». Implémentation : `AaveTraceExecutor.withdraw_all()`,
appelée par `tracer._fire_aave_cycle()`.

Réserve pour M2 : `withdraw_all` vide **tout** le collatéral USDC. C'est juste
pendant M1 où le traceur est le seul déposant, mais dès que le coussin USDC
réel existe, un cycle devra retirer son seul dépôt — lire le solde d'aToken
on-chain et le passer à `withdraw`, ce qui est sûr dans ce sens puisque le
solde ne fait que croître avec les intérêts.

## 3 bis. anvil récent ne forke plus Arbitrum sans `--hardfork`

anvil 1.8.1 échoue avec `Excess blob gas not set.` dès le premier `eth_call`
sur un fork d'Arbitrum : les blocs Arbitrum ne portent pas les champs blob que
l'EVM post-Cancun attend. Ajouter `--hardfork shanghai` à la commande de fork.

## 4. Rejouer le precheck

```
anvil --fork-url https://arb1.arbitrum.io/rpc --port 8545 --chain-id 42161 --hardfork shanghai
uv run python scripts/precheck_aave_fork.py
```

Le script imprime le gas par opération. Le relancer après toute modification
du code Aave (`src/delta0/executor.py`, `src/delta0/venues/aave.py`) : c'est
le seul filet avant de toucher au mainnet.

Foundry (anvil, cast, forge) est installé en v1.8.1 dans
`%LOCALAPPDATA%/foundry/bin/`, ajouté au PATH utilisateur.

## 5. Wallet opérateur

`0x4F7ed211FcEF5555B0EC309E3bFfcCfE27750C89` — float opérationnelle de la
marche à blanc (README §14 : ~100 $, 50 USDC + réserve de gas). Au moment du
fork il portait 187 USDC + 0,005 ETH.

## 6. Coût en gaz d'un cycle traceur

Mesuré sur le fork le 2026-09-02 : **932 479 gas** par cycle complet
(55 437 + 237 854 + 240 995 + 55 437 + 169 615 + 173 141).

Sur 7 jours à raison d'un cycle toutes les 30 min (336 cycles) :

| Prix du gaz | ETH consommé | ~USD |
|---|---|---|
| 0,01 gwei (plancher habituel) | 0,0031 ETH | ~7 $ |
| 0,05 gwei | 0,0157 ETH | ~37 $ |
| 0,10 gwei (congestion) | 0,0313 ETH | ~75 $ |

Conséquence : les 0,005 ETH de la float initiale ne couvrent pas 7 jours dès
que le gaz dépasse son plancher. Prévoir ~0,02-0,03 ETH avant une session
longue.

## 7. Le gaz doit porter une marge — sinon la tx revert

Premier cycle live (2026-09-02) : le `repay` a reverté. Ni ABI, ni allowance,
ni logique Aave — **manque de gaz**.

```
repay qui a reverte  : limite 168 594  (= estimate_gas brut)
repay qui a reussi   : 169 810 consommes
```

`web3.contract.build_transaction` remplit le champ `gas` avec le retour
d'`eth_estimateGas` sans y ajouter la moindre marge. Sur Arbitrum cette
estimation melange le cout d'execution L2 et un cout de publication L1 derive
de la base fee L1 du moment — ce second terme bouge entre l'estimation et
l'inclusion. Un ecart de 0,6 % a suffi.

Deux pieges de diagnostic rencontres :
- `eth_call` de rejeu **ne revert pas** : un call n'est pas contraint par la
  limite de gaz de la transaction. Une simulation verte ne disculpe pas le gaz.
- le `gasUsed` du recu (163 761) etait **inferieur** a la limite, ce qui semble
  exclure l'epuisement. Arbitrum comptabilise la part L1 a part : ne jamais
  conclure "ce n'est pas le gaz" a partir de `gasUsed < gasLimit` sur un L2.

Solution : `delta0.gas.with_gas_margin()`, marge de 35 %, appliquee dans
`executor._journal_and_send` et `venues/bridge`. Le gaz non consomme est
rembourse : la marge ne coute rien.

Consequence en cascade : le `repay` rate laisse la dette ouverte, donc le
`withdraw` suivant echoue avec `HealthFactorLowerThanLiquidationThreshold`
(0x6679996d) et la position reste ouverte. D'ou `scripts/unwind_aave.py`,
a lancer a la main pour refermer ce que le traceur a laisse en plan.

## 8. Selecteurs d'erreurs Aave v3 rencontres

```
0x47bc4b2c  NotEnoughAvailableUserBalance()               withdraw d'un montant exact
0x6679996d  HealthFactorLowerThanLiquidationThreshold()   withdraw avec dette ouverte
```

Aave v3 utilise des erreurs personnalisees : web3 ne remonte que le selecteur.
Pour en decoder un nouveau, hacher les signatures candidates en keccak et
comparer les 4 premiers octets.
