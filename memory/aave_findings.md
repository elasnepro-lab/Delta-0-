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
withdraw(USDC, montant, self)
```

Invariant vérifié en fin de séquence : `solde USDC final == solde USDC initial`
(aux frais de gas près, payés en ETH). C'est le test qui prouve que le cycle
traceur est bien un aller-retour neutre.

`rateMode = 2` = taux variable. Le taux stable est déprécié sur Aave v3.

## 4. Rejouer le precheck

```
anvil --fork-url https://arb1.arbitrum.io/rpc --port 8545 --chain-id 42161
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
