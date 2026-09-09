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

## 9. Parametres de reserve lus on-chain (chantier 0.1, bloc 503134105)

Lecture du 2026-09-08 sur `AaveProtocolDataProvider`
(`0x7F23D86Ee20D869112572136221e173428DD740B`), reproductible avec
`scripts/read_aave_params.py`.

```
wstETH  LTV max 0.7500   LT 0.7900   bonus 1.0720 (penalite 7,2 %)
        frais de protocole 10 %   collateral oui   emprunt non
        actif, non gele, non en pause
        supply cap 34 000 (18 217 deposes, 53,6 %)   marge 15 783
USDC    LTV max 0.7500   LT 0.7800   bonus 1.0500
        borrow cap 225 000 000 (141 724 563 empruntes, 63,0 %)
```

**Le LT du wstETH vaut exactement 0,79.** Les seuils du YAML sont donc faux :

```
ltv_pump        0.75   marge au LT +0.04   OK
ltv_cushion     0.79   marge au LT  0.00   EST le seuil de liquidation
ltv_deleverage  0.81   marge au LT -0.02   AU-DELA de la liquidation
```

P4 ne peut mathematiquement pas se declencher avant la liquidation. La bande
basse reelle est de **-11,39 %** depuis LTV 0,70 (le classeur annonce -15,7 %,
valeur qui correspond a un LT de 0,81 : celui du wstETH sur Ethereum, pas sur
Arbitrum).

E-mode : trois categories existent (Stablecoins, ETH correlated,
ezETH/wstETH/WETH), toutes en LTV 0,93 / LT 0,95, mais une dette USDC contre un
collateral wstETH n'y est pas eligible. La strategie tourne donc en e-mode 0,
LT 0,79. Le champ `getReserveEModeCategory` n'existe pas sur cette version du
data provider.

Caps : sans contrainte a l'echelle du chassis 20 000 $ (il faut ~16,5 wstETH
pour 15 783 disponibles). A verifier au boot malgre tout, pas avant.

## 10. `stEthPerToken()` n'existe pas sur le wstETH d'Arbitrum

Les trois fonctions de taux de Lido revertent sur
`0x5979D7b546E38E414F7E9822514be443A4800529` : `stEthPerToken()`,
`tokensPerStEth()`, `getStETHByWstETH()`. C'est un jeton ponte, il ne porte pas
le taux de conversion — celui-ci vit sur L1.

Consequence : le ratio doit venir de l'oracle Aave lui-meme, ce qui est de toute
facon preferable puisque c'est le prix qui fait foi pour le HF.

```
AaveOracle  0xb56c2F0B653B2e0b10C9b928C8580Ac5Df02C7C7   (base 1e8)
getAssetPrice(wstETH) = 3 093,02      source 0xb4a28DF1b926646f94e6fE6f15828c491b4def5F
getAssetPrice(WETH)   = 2 487,23      source 0xbD41b1548a5A06544cBcf87c0c54864312842C00
ratio wstETH/ETH = 3093,02 / 2487,23 = 1,243559
```

Erreur actuelle du snapshot (`watcher.py:127` pose
`wsteth_price_usd = mark_price`), au prix du jour :

```
16,5 wstETH  reel 51 035 $   vu par le bot 41 039 $   ecart -19,59 %
LTV sur 35 000 $ de dette :  reel 0,6858   vu par le bot 0,8528
```

Le bot verrait donc **0,8528 sur une position saine a 0,6858**, soit au-dessus
du LT de 0,79 : des le premier cycle apres BUILD, il declencherait P4 sur une
position qui n'a aucun probleme.

## 11. Le coussin se leviérise lui-même

Constat de conception, tire du calcul des bandes le 2026-09-09 avec LT 0,79.

Le coussin est de l'USDC depose sur Aave. Il compte donc dans le collateral, et
a LTV cible constant il **porte de la dette supplementaire**. Chassis a capital
20 000 constant, cible 0,70 :

```
coussin    spot    dette   bande (coussin plein)   bande (coussin consomme)
 1 000   49 250   35 175          -11,62 %                 -9,59 %
 2 000   48 500   35 350          -11,86 %                 -7,74 %
 3 500   47 375   35 612          -12,23 %                 -4,85 %
```

Grossir le coussin achete quelques dixiemes de point tant qu'il est plein, et en
coute plusieurs une fois vide — **au moment precis ou P3 vient de s'en servir**.
La defense se paie en marge de securite, et plus le coussin est gros, plus elle
coute cher.

Deux consequences :

1. Le solveur ne doit pas dimensionner la dette sur un collateral qui inclut le
   coussin (chantier 1.3, point K10). Sinon le coussin finance sa propre dette.
2. Le classeur doit afficher **les deux bandes**, pleine et consommee. La seconde
   est celle qui vaut apres la premiere tranche de P3, donc celle qui compte.

Note sur les chiffres publies : la bande depend du LTV de depart. Le chassis
d'illustration de l'audit (spot 50 000, dette 35 000, coussin 1 000) est en fait
a LTV 0,686 et non 0,70 — le coussin adoucit le ratio — d'ou son -13,39 %. Un
chassis reellement construit a 0,70 donne -11,62 %. Les deux sont justes ; ils ne
partent pas du meme point. Que le chassis de reference ne soit pas a sa propre
cible est le point K10.

## 12. Arbitrage chiffre de la cible LTV

Meme modele, capital 20 000, coussin 1 000, levier short 10x :

```
cible    exposition   bande    coussin vide   carry relatif
0,700       2,51     -11,62 %     -9,59 %        100,0 %
0,675       2,36     -14,87 %    -12,71 %         94,0 %
0,650       2,23     -18,13 %    -15,84 %         88,7 %
```

Descendre de 0,70 a 0,65 achete **6,5 points de bande** contre 11 % de carry,
soit environ 575 $/an sur les 5 100 $ bruts attendus. A comparer aux 1 300 $
d'une liquidation « propre » (point A3 de l'audit), sachant que l'ETH fait -13 %
en une journee plusieurs fois par an. Le depeg stETH (F7) s'ajoute a cette
bande, il ne s'en deduit pas.

Decision a prendre au chantier 1.5. Elle remet en cause la decision figee n° 3.
