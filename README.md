# Bot "Montage C" : portage delta-neutre wstETH / short ETH

Spécification complète pour implémentation. Ce document est la source de vérité : toute décision de code qui contredit ce document est un bug. Langue du code : identifiants et commentaires en anglais, logs et alertes en français.

Paramètres sources : classeur `montage_C_delta_neutre.xlsx`, feuille "Model C", validé par l'opérateur le 25/08/2026. Toute divergence entre ce README et le classeur se résout en faveur du classeur, puis ce README est mis à jour.

---

## 1. Objectif et stratégie (résumé)

Le bot exploite une position delta-neutre : du wstETH en collatéral sur Aave v3 (Arbitrum), financé partiellement par une boucle d'emprunt USDC, couvert par un short ETH-PERP de notionnel équivalent sur Hyperliquid (marge isolée). L'exposition nette au prix de l'ETH est nulle. Les revenus : funding du short (payé chaque heure) + rendement de staking du wstETH. Les coûts : intérêts d'emprunt USDC + frais + accidents.

Le bot n'est PAS un trader : il ne prend aucune décision directionnelle. Ses seules missions :
1. Construire et déconstruire la position selon la "porte de régime" (moyenne 30 jours du funding).
2. Maintenir les ratios cibles quand le prix bouge (re-centrage).
3. Empêcher toute liquidation, sur les deux flancs (pompe, coussin, réduction d'urgence).
4. Écrémer le funding accumulé selon la politique de distribution (v1 : recomposition, voir 8.5).
5. Tout journaliser et s'arrêter proprement quand il est aveugle.

Principe cardinal de sécurité : **le pont sert au confort, jamais à la survie.** Toute action de survie doit passer par un chemin local exécutable en secondes.

Particularité du châssis choisi : les bandes de liquidation sont volontairement asymétriques (−15,7 % / +8,0 %). Le flanc étroit (hausse) est celui qui dispose des défenses les plus rapides (pompe montante ~2 min, réduction ~2 s) ; le flanc large (baisse) est celui dont la pompe est lente (~6 min). Cette asymétrie est un choix de conception, pas un accident : ne pas la "corriger".

La performance vient de l'exécution, pas de l'idée : le rendement est un fait de marché non prédictible, le métier du bot est l'exactitude. L'exactitude se mesure sur cinq dimensions (justesse, fidélité, vitesse, robustesse, coût), définies en section 12 et testées en section 15.

---

## 2. Glossaire

| Terme | Définition |
|---|---|
| Notionnel | Taille du short × prix mark, en USD. Base de calcul du funding. |
| Delta | Valeur spot (wstETH en USD) moins notionnel du short. Cible : 0. |
| LTV | Dette totale USD / collatéral total USD sur Aave. Cible : 70 %. |
| HF (health factor) | (Collatéral × seuil de liquidation) / dette, lu on-chain via Aave. Liquidation si HF < 1. |
| Margin ratio | Marge isolée / notionnel du short sur Hyperliquid. Cible : 10 % (levier 10x). |
| Anchor | Prix ETH du dernier re-centrage. Référence des seuils de re-centrage. |
| Re-centrage | Remise des ratios à leur cible après un mouvement de prix au-delà du seuil (asymétrique, voir config). |
| Pompe | Transfert de collatéral entre Aave et Hyperliquid via le pont (2 min aller, 6 min retour). |
| Coussin | USDC déposé en supply sur Aave, réserve de secours à 5 secondes. |
| Réduction | Fermeture partielle du short en urgence (2 secondes, sans pont). |
| Écrémage | Traitement hebdomadaire de l'excédent de marge (v1 : recomposition). |
| Recomposition | Politique de réinvestissement : re-ciblage global de la machine à ratios constants (voir 8.5). |
| Porte de régime | Règle qui fixe l'exposition (2,5x / 1,5x / 0) selon la moyenne 30 j du funding. |
| BLIND | État dégradé : le bot ne peut plus lire ou agir de façon fiable. Réaction : dégonfler, pas attendre. |
| TRACER | Mode d'exécution de la marche à blanc : seules des micro-opérations de mesure sont autorisées. |

---

## 3. Architecture

Quatre modules + un état persistant. Python 3.11+, asyncio.

```
src/
  watcher.py      # lectures : WS Hyperliquid (mark price, funding, fills, user events),
                  # RPC Arbitrum (HF, LTV, soldes, événements de liquidation Aave)
  decision.py     # moteur de décision PUR (aucun I/O) : état observé -> action ou None.
                  # Contient le solveur d'état cible. Testable unitairement à 100 %.
  executor.py     # exécution des actions : tx Aave, ordres HL, pont, swaps.
                  # Chemins pré-armés (approvals faites), idempotent, journalise avant d'agir.
  watchdog.py     # mesure les latences réelles (p50/p95), détecte l'état BLIND,
                  # déclenche le dégonflage de sécurité, gère le kill-file.
  state.py        # persistance (SQLite) : journal d'intentions, anchor, régime, compteurs.
  venues/
    hyperliquid.py  # wrapper SDK officiel (info + exchange + WS)
    aave.py         # wrapper web3 : Pool, DataProvider, aTokens
    swap.py         # agrégateur (Odos ou 1inch), garde-fou slippage
    bridge.py       # dépôt Bridge2, retrait via API HL, suivi de crédit
  main.py         # boucle principale, câblage, modes (DRY_RUN / LIVE_SMALL / LIVE)
config.yaml
tests/
backtest/         # rejeu court (M2) et backtest long (M2b) : données, splicing, rapports
```

Règle d'or : `decision.py` ne fait AUCUN appel réseau. Il reçoit un snapshot d'état complet et retourne une action typée avec sa priorité. C'est ce qui rend le bot testable et les décisions reproductibles.

Solveur d'état cible : une fonction unique `target_state(equity, config) -> {spot_target, notional_target, margin_target, debt_target}` avec spot_target = equity × exposure_mult, notional_target = spot_target, margin_target = spot_target × target_margin_ratio, debt_target = target_ltv × collateral_target. BUILD, RECENTER, SKIM et DEFLATE sont tous des convergences vers cet état cible ; seules les urgences P1 à P4 y dérogent.

---

## 4. Paramètres de configuration

Tous les paramètres vivent dans `config.yaml`. Aucune constante métier en dur dans le code. Les paramètres de risque Aave (LT, LTV max) sont LUS on-chain au démarrage ; les valeurs du YAML ne servent que de garde-fou de cohérence (alerte si écart).

```yaml
capital_usd: 20000            # capital initial de référence (classeur Model C)
short_leverage: 10            # levier du short HL (marge isolée)
target_ltv: 0.70              # LTV cible Aave
target_margin_ratio: 0.10     # = 1 / short_leverage
exposure_mult: 2.5            # = 1 / (1 - target_ltv + 1/short_leverage) ; recalculé et vérifié au boot
exposure_mult_half: 1.5       # exposition en régime intermédiaire
maintenance_margin: 0.02      # maintenance HL estimée ; lue via API au boot, alerte si écart
cushion_pct: 0.05             # coussin USDC en supply Aave (% du capital) = 1 000 $
cushion_floor_pct: 0.025      # sous ce niveau : reconstitution prioritaire à l'écrémage
recenter_up: 0.045            # seuil de re-centrage à la hausse (bande haute étroite : +8 %)
recenter_down: 0.06           # seuil de re-centrage à la baisse (bande basse large : -15,7 %)
delta_tolerance: 0.02         # |delta| / spot max toléré avant re-truage du short
skim_cron: "SUN 12:00 UTC"    # écrémage hebdomadaire
skim_min_usd: 200             # sous ce montant, l'écrémage attend la semaine suivante
skim_policy: recompose        # recompose | deleverage | dividend (v1 : recompose)
regime:
  spread_full_bps: 500        # funding30j >= borrow + 5 pts -> exposition pleine
  hysteresis_days: 7          # confirmation avant tout changement de régime
slippage_max_bps: 30
order_style: maker_then_cross # ALO 60 s puis traversée du spread ; urgences : IOC direct
gas_min_eth: 0.003            # réserve de gas Arbitrum, blocage des ops non critiques si dessous
emergency:
  margin_ratio_pump: 0.05     # déclenche la pompe montante (prix ~ +5 % depuis l'anchor)
  margin_ratio_reduce: 0.035  # déclenche la réduction d'urgence (2 s), ~1,5 pt de prix avant liquidation
  reduce_fraction: 0.30       # part du short fermée par réduction
  ltv_pump: 0.75              # déclenche la pompe descendante (prix ~ -6,7 %)
  ltv_cushion: 0.79           # déclenche le remboursement depuis le coussin (prix ~ -11,4 %)
  ltv_deleverage: 0.81        # déclenche le désendettement par étapes (prix ~ -13,6 %)
watchdog:
  ws_stale_s: 10
  rpc_fail_s: 30
  tx_fail_max: 3
  latency_budget_factor: 1.5  # si p95 mesuré > budget x facteur : re-centrage anticipé (0.03 / 0.045)
venues:
  arbitrum_rpc: [primary_url, fallback_url]
  usdc_address: "0xaf88...5831"   # USDC NATIF uniquement, jamais USDC.e
  aave_pool: "..."                # adresses officielles Aave v3 Arbitrum
  hl_api: "https://api.hyperliquid.xyz"
alerts:
  telegram_bot_token: env:TG_TOKEN
  telegram_chat_id: env:TG_CHAT
mode: DRY_RUN                  # DRY_RUN | LIVE_SMALL | LIVE
live_small_cap_pct: 0.10       # plafond de capital en mode LIVE_SMALL (2 000 $)
```

### Bilan de référence attendu après BUILD (assertions des tests M3)

| Poste | Valeur cible | Tolérance |
|---|---|---|
| wstETH déposé sur Aave | 50 000 $ | ± 1 % |
| Marge USDC sur Hyperliquid | 5 000 $ | ± 1 % |
| Dette USDC totale | 35 000 $ | ± 1 % |
| Notionnel du short | 50 000 $ | ± delta_tolerance |
| LTV | 70,0 % | ± 0,5 pt |
| Margin ratio | 10,0 % | ± 0,5 pt |
| Bandes de liquidation | −15,7 % / +8,0 % | recalculées depuis les paramètres lus on-chain |
| Coussin USDC (supply Aave) | 1 000 $ | ± 1 % |

Hypothèses économiques de référence du classeur (pour le module comptable et les simulations, pas pour les décisions) : staking 2,7 %, emprunt USDC 5 %, funding moyen retenu 11 %, frais 40 $/mois, provision accidents 250 $/mois, croissance recomposition théorique 25,5 %/an.

---

## 5. Grandeurs calculées et formules

Le watcher produit un snapshot unique et daté. Toutes les formules ci-dessous sont implémentées dans `decision.py` et testées unitairement.

```
spot_usd        = wsteth_atoken_balance * wsteth_price_usd
cushion_usd     = usdc_atoken_balance
collateral_usd  = spot_usd + cushion_usd
debt_usd        = usdc_variable_debt_balance
ltv             = debt_usd / collateral_usd
hf              = lu directement via Pool.getUserAccountData (ne pas recalculer)
notional_usd    = short_size_eth * mark_price
margin_ratio    = isolated_margin_usd / notional_usd
delta_usd       = spot_usd - notional_usd
delta_pct       = delta_usd / spot_usd
price_move      = (mark_price - anchor_price) / anchor_price
funding_30d     = moyenne(funding horaire sur 720 h) * 8760      # annualisé
borrow_apr      = taux variable USDC lu on-chain (ray -> apr)
carry_spread    = funding_30d - borrow_apr
equity          = collateral_usd + isolated_margin_usd - debt_usd
exposure_mult   = 1 / (1 - target_ltv + 1/short_leverage)        # vérifié contre config au boot
```

---

## 6. Machine à états

```
INIT -> DRY_RUN -> BUILDING -> RUNNING
RUNNING <-> RECENTERING          (opération planifiée, minutes)
RUNNING -> EMERGENCY -> REPAIRING -> RUNNING
RUNNING -> DEFLATING -> PARKED   (porte de régime fermée : wstETH sans dette, pas de short)
PARKED  -> BUILDING              (porte rouverte, hystérésis respectée)
tout état -> BLIND -> SAFE_DEFLATE -> PARKED ou STOPPED
tout état -> UNWINDING -> STOPPED (ordre opérateur)
```

Règles d'état :
- Une seule opération d'exécution à la fois (verrou global). Les urgences préemptent tout.
- Chaque transition est journalisée avec son motif et le snapshot déclencheur.
- Au démarrage, le bot ne fait JAMAIS confiance à sa mémoire : il reconstruit l'état réel depuis la chaîne et l'API (réconciliation), compare au journal, et alerte sur tout écart.

---

## 7. Table de décision (ordre de priorité strict)

Le moteur évalue de haut en bas et retourne la première action déclenchée. Latence max = budget d'exécution, mesuré par le watchdog. Échelle des déclencheurs côté hausse : re-centrage +4,5 %, pompe ~+5 %, réduction ~+6,5 %, liquidation +8 %. Côté baisse : re-centrage −6 %, pompe ~−6,7 %, coussin ~−11,4 %, désendettement ~−13,6 %, liquidation −15,7 %.

| P | Condition | Action | Chemin | Latence max |
|---|---|---|---|---|
| 1 | Événement de liquidation détecté (Aave ou HL) | Couper le short pour égaler le spot restant, puis REPAIRING | local HL | 2 s |
| 2 | margin_ratio <= 0.035 | Réduction : fermer reduce_fraction du short (IOC) | local HL | 2 s |
| 3 | ltv >= 0.79 | Rembourser une tranche depuis le coussin | local Aave | 10 s |
| 4 | ltv >= 0.81 et coussin < tranche | Désendettement par étapes : repay coussin restant -> withdraw wstETH -> swap -> repay, en boucle | local Aave | 60 s |
| 5 | margin_ratio <= 0.05 | Pompe montante : borrow -> bridge -> add margin | pont | 3 min |
| 6 | ltv >= 0.75 | Pompe descendante : withdraw HL -> bridge -> repay | pont | 8 min |
| 7 | price_move >= +0.045 ou <= -0.06 | Re-centrage complet (procédure 8.3 ou 8.4) | pont | 15 min |
| 8 | abs(delta_pct) > 0.02 | Re-truage du short vers notional_target | local HL | 60 s |
| 9 | Cron écrémage atteint et excédent > skim_min_usd | Écrémage-recomposition (procédure 8.5) | pont | sans enjeu |
| 10 | Changement de régime confirmé (hystérésis) | BUILDING ou DEFLATING par étapes | pont | jours |

Les priorités 1 à 4 sont les seules autorisées en état BLIND partiel (selon la venue joignable).

---

## 8. Procédures détaillées

### 8.1 BUILD (construction)
Précondition : porte de régime OUVERTE (carry_spread >= spread_full_bps confirmé 7 jours), mode != DRY_RUN.
1. Vérifier : e-mode Aave désactivé (`setUserEMode(0)`), approvals en place, gas >= gas_min_eth, USDC natif.
2. Déposer le coussin : supply USDC (cushion_pct × capital = 1 000 $).
3. Construire en 3 tranches de taille égale. Pour chaque tranche :
   a. Boucle itérative jusqu'à l'exposition de tranche : supply wstETH -> borrow USDC -> swap USDC->wstETH -> supply. 3 itérations max par tranche, slippage <= slippage_max_bps.
   b. Borrow la marge de tranche (target_margin_ratio × notionnel de tranche), bridge vers HL.
   c. Dès crédit du pont : ouvrir le short de tranche (maker, timeout 60 s, puis traversée), levier 10x isolé.
   d. Vérifier delta de tranche <= tolérance avant la tranche suivante. Jamais plus de 10 minutes entre le spot et son short.
4. Poser l'anchor = prix mark. Vérifier les invariants (section 11) et le bilan de référence (section 4). Passer RUNNING.

### 8.2 Sortie complète (UNWIND)
Ordre inverse strict : fermer le short (maker par tranches) -> retirer la marge -> bridge retour -> rembourser toute la dette -> dérouler la boucle (withdraw -> swap -> repay itératif) -> retirer coussin et wstETH. État final : wstETH libre, zéro dette, zéro position.

### 8.3 RECENTER_UP (prix >= anchor × 1,045)
1. Calculer les cibles via le solveur d'état cible (equity courante).
2. Borrow le complément de marge sur Aave (la hausse a libéré la capacité), vérifier ltv_after <= target_ltv + 0,01.
3. Bridge vers HL, add margin isolée.
4. Ajuster la taille du short vers notional_target (maker).
5. anchor = prix mark. Journaliser le coût complet de l'opération.

### 8.4 RECENTER_DOWN (prix <= anchor × 0,94)
1. Calculer les cibles via le solveur d'état cible.
2. Retirer l'excédent de marge HL au-dessus de margin_target, bridge retour, repay la dette.
3. Réduire la taille du short vers notional_target.
4. anchor = prix mark.

### 8.5 SKIM-RECOMPOSITION (écrémage hebdomadaire, politique v1)
1. excess = marge HL - margin_target. Si excess < skim_min_usd : ne rien faire (frais fixes).
2. Retirer excess, bridge retour.
3. Affecter dans l'ordre :
   a. Reconstitution du coussin jusqu'à cushion_pct × capital courant.
   b. Recomposition par re-ciblage global (true-up) : recalculer equity (= collateral + marge + solde - dette), obtenir l'état cible complet via le solveur, puis exécuter les ÉCARTS entre l'état courant et ces cibles (achat de wstETH financé par le solde plus le complément d'emprunt, appoint de marge, agrandissement du short). Cette formulation absorbe en une seule opération le funding à réinvestir ET la dérive de la semaine (intérêts courus qui poussent la LTV vers le haut, staking qui la tire vers le bas) : après l'opération, toute la machine, pas seulement l'incrément, est revenue à LTV 70 % et marge 10 %.
4. Si la porte de régime n'est pas OUVERTE : la recomposition est suspendue, le solde part en remboursement de dette (politique deleverage par défaut en régime non confirmé).
5. Journaliser la ligne comptable (funding encaissé, intérêts courus, net, capital courant recalculé).

### 8.6 EMERGENCY_REDUCE (P2)
1. Ordre IOC : fermer reduce_fraction du short. Pas de maker, pas d'attente.
2. Alerte CRITICAL. Passer REPAIRING.
3. REPAIRING (une fois margin_ratio >= 0.07 et volatilité 5 min < 2 %) : vendre la tranche de wstETH excédentaire (withdraw -> swap), repay dette, re-truer le delta, re-poser l'anchor.

### 8.7 EMERGENCY_REPAY et désendettement (P3, P4)
Tranche standard : 25 % du coussin initial. P3 : withdraw coussin -> repay. P4 : si coussin insuffisant, boucle locale : repay ce qui reste -> withdraw wstETH rendu disponible -> swap -> repay, jusqu'à ltv <= target_ltv + 0,01. Puis re-truage du short (le spot a diminué).

### 8.8 LIQUIDATION DÉTECTÉE (P1)
Détection : événement `LiquidationCall` Aave (filtre sur l'adresse du bot) ou événement de liquidation HL sur le user feed.
1. Sous 2 secondes : ramener le short à la taille du spot restant (IOC).
2. Geler toute autre opération, alerte CRITICAL, dump complet de l'état.
3. REPAIRING manuel uniquement : le bot attend une commande opérateur (post-mortem obligatoire).

### 8.9 Porte de régime (P10)
Évaluée une fois par jour à 00:00 UTC sur funding_30d et borrow_apr :
- carry_spread >= spread_full_bps pendant hysteresis_days -> cible 2,5x.
- 0 <= carry_spread < spread_full_bps pendant hysteresis_days -> cible 1,5x.
- carry_spread < 0 pendant hysteresis_days -> cible 0 (DEFLATING vers PARKED).
Tout changement d'exposition se fait par tranches de 25 % de l'écart, une tranche par heure maximum, via les procédures 8.1/8.2 partielles. Jamais de changement d'exposition en urgence.

---

## 9. Intégrations techniques

### 9.1 Hyperliquid
- SDK Python officiel (`hyperliquid-python-sdk`). REST info + exchange, WebSocket pour mark price, funding, fills et user events.
- Marge ISOLÉE obligatoire sur ETH-PERP, levier short_leverage (10x). Vérifier au boot, corriger si besoin. Lire la maintenance margin réelle via l'API et alerter si écart avec maintenance_margin.
- Ordres : maker (ALO) avec timeout 60 s puis traversée du spread pour les opérations planifiées ; IOC pour P1/P2. Gérer les fills partiels : re-coter le reliquat, jamais considérer un ordre comme atomique.
- Funding : endpoint funding history pour la moyenne 30 j ; crédité chaque heure dans la marge, aucun traitement requis à part le suivi comptable.
- Clé : wallet dédié au bot. Les ordres passent par un agent wallet (clé séparée, sans droit de retrait) ; les retraits exigent la signature du wallet maître. Les deux clés en variables d'environnement, jamais dans le code ni le journal.
- Contraintes : dépôt minimum 5 USDC, retrait minimum 2 USDC, frais de retrait 1 USDC, notionnel minimum ~10 $ par ordre, USDC natif Arbitrum uniquement.

### 9.2 Aave v3 (Arbitrum)
- Contrats : Pool (supply, borrow taux variable, repay, withdraw, getUserAccountData), ProtocolDataProvider (paramètres de risque lus au boot), aTokens et variableDebtToken pour les soldes.
- E-mode : désactivé, vérifié au boot (l'e-mode ETH interdirait l'emprunt USDC).
- Approvals ERC-20 (USDC et wstETH vers le Pool, USDC vers le pont HL) : posées une fois au setup, montant plafonné, re-vérifiées au boot. Le chemin critique doit toujours être UNE transaction.
- HF et LTV : toujours lus on-chain, jamais recalculés localement pour les décisions P1-P6 (le calcul local sert de contrôle de cohérence).

### 9.3 Pont
- Aller (Arbitrum -> HL) : transfert USDC natif vers Bridge2, crédité après finalité (~1 à 3 min). Surveiller le crédit via l'API avant toute étape suivante.
- Retour (HL -> Arbitrum) : retrait via API (signature maître), validateurs + fenêtre de contestation (~4 à 8 min).
- Chaque traversée est un "transfert en transit" journalisé avec un ID ; un transfert non crédité après 15 min déclenche une alerte WARN, après 60 min CRITICAL et gel des opérations dépendantes.

### 9.4 Swaps
- Agrégateur (Odos ou 1inch) avec quote préalable, slippage_max_bps strict, deadline courte. Tout swap dont le quote dévie de plus de slippage_max_bps est abandonné et re-coté. Paires : USDC <-> wstETH uniquement.

---

## 10. Sécurité et clés

- Wallet dédié à la machine, capital plafonné : rien d'autre ne vit sur cette adresse.
- Clés (wallet maître, agent HL, RPC payant, Telegram) : variables d'environnement ou secret manager, jamais commitées, jamais loggées.
- Serveur : VPS dédié, accès SSH par clé, mises à jour automatiques, horloge NTP (les signatures HL sont sensibles au temps).
- RPC : fournisseur payant + fallback public, bascule automatique.
- Kill-file : la présence d'un fichier `KILL` à la racine met le bot en pause propre (aucune nouvelle action, positions inchangées) ; `KILL_DEFLATE` déclenche SAFE_DEFLATE.
- Aucune interface web exposée. Contrôle par CLI locale et Telegram (lecture seule + commandes limitées : pause, resume, deflate, status).

---

## 11. Watchdog, modes dégradés, invariants

### Watchdog
- Mesure en continu : latence WS (fraîcheur du dernier tick), latence RPC, temps de confirmation tx, durées réelles de pont (aller et retour). Conserve p50/p95 glissants sur 7 jours.
- BLIND si : WS muet > ws_stale_s, ou RPC en échec > rpc_fail_s, ou tx_fail_max transactions consécutives échouées.
- En BLIND : seules les priorités 1 à 4 restent autorisées, sur la venue encore joignable. Si HL seul joignable : réduire le short à 50 % et geler. Si Aave seul joignable : rembourser depuis le coussin et geler. Si aucune : alerte CRITICAL en boucle, aucune action.
- Si p95 mesuré d'un chemin > budget × latency_budget_factor : mode prudent, re-centrage anticipé à +3 % / -4,5 % au lieu de +4,5 % / -6 %.

### Invariants (vérifiés à chaque snapshot, violation = alerte + action de la table)
```
I1  abs(delta_pct) <= 0.02 en RUNNING stable
I2  ltv <= 0.72 en croisière ; jamais >= 0.79 plus de 5 min sans action P3 déclenchée
I3  margin_ratio >= 0.07 en croisière ; jamais <= 0.035 sans action P2 déclenchée
I4  cushion_usd >= cushion_floor_pct * capital courant (sinon reconstitution prioritaire au prochain écrémage)
I5  gas_eth >= gas_min_eth (sinon blocage des opérations non critiques + alerte)
I6  aucun transfert en transit > 15 min sans alerte
I7  une seule opération d'exécution en cours à tout instant
I8  après chaque écrémage-recomposition : ltv et margin_ratio de retour aux cibles à ±0,5 pt
```

---

## 12. Journalisation, alertes, comptabilité, tableau d'exactitude

- Journal structuré (JSON lines) : chaque snapshot décisionnel, chaque intention, chaque tx (hash, gas, statut), chaque fill, chaque traversée de pont, chaque alerte.
- Alertes Telegram à trois niveaux : INFO (digest quotidien), WARN (invariant mou violé, pont lent, re-centrage exécuté), CRITICAL (P1 à P4 déclenchés, BLIND, transfert perdu).
- Export comptable : CSV quotidien des flux (funding, intérêts courus, staking estimé, frais, écrémages, recompositions) avec cumuls mensuels. Base du suivi de performance et de la déclaration fiscale.

Le digest quotidien EST le tableau d'exactitude, cinq dimensions, cinq chiffres, rien d'autre :

| Dimension | Métrique | Seuil |
|---|---|---|
| Justesse (il décide juste) | déclenchements attendus vs réalisés, actions interdites émises | 100 %, zéro |
| Fidélité (il exécute juste) | delta post-opération, ratios restaurés (I8), slippage vs devis | ±2 %, ±0,5 pt, <= 30 bps |
| Vitesse (il agit à temps) | p95 de chaque chemin vs budget (2 s / 10 s / 3 min / 8 min) | p95 <= budget |
| Robustesse (il survit à lui-même) | réconciliations propres, écarts inexpliqués, états silencieux | zéro |
| Coût (il tient son budget) | frais + accidents réalisés vs provision (40 + 250 $/mois) | <= provision en cumul |

---

## 13. Persistance, reprise, idempotence

- SQLite : table `intents` (id, action, params, statut : pending/sent/confirmed/failed), table `state` (anchor, régime, mode, capital courant), table `transfers` (ponts en transit), table `metrics`.
- Toute action est écrite en `pending` AVANT le premier appel réseau, puis mise à jour. Au redémarrage : réconciliation complète (soldes on-chain + état HL) contre le journal ; toute intention `sent` non retrouvée on-chain est investiguée avant toute nouvelle action.
- Idempotence : chaque intention porte un ID déterministe ; rejouer une intention confirmée est un no-op. Nonces gérés explicitement, jamais deux tx Aave en vol.

---

## 14. Modes d'exécution et plan de développement

| Étape | Livrable | Critère d'acceptation |
|---|---|---|
| M0 | Squelette, config, connexions lecture seule aux deux venues | `status` affiche un snapshot complet et juste |
| M1 | Watcher + watchdog + rapport de latences (mode TRACER) | 7 jours de DRY_RUN, rapport p50/p95 des 5 chemins critiques + journal des tirs à blanc |
| M2 | Moteur de décision + rejeu court (30 j) en paper | actions attendues aux bons moments, zéro action interdite |
| M2b | Backtest long (voir 15.3) | zéro liquidation simulée avec pompe p95, rapport de régimes, A/B porte ON/OFF |
| M3 | Executor complet en LIVE_SMALL (10 % du capital, soit 2 000 $) | 2 semaines : au moins 1 re-centrage réel chaque sens + 1 écrémage-recomposition, zéro violation d'invariant, bilan conforme à la section 4 |
| M4 | LIVE pleine taille + runbook validé | Bascule après revue du journal M3 |

Le mode DRY_RUN est obligatoire et bloquant : le bot refuse de passer LIVE sans un rapport de latences M1 de moins de 30 jours. LIVE_SMALL plafonne le capital déployé à live_small_cap_pct.

Précision M1 : la marche à blanc signifie zéro position, pas zéro fonds. Le wallet reçoit une float opérationnelle (~100 $ : 50 USDC + réserve de gas) servant de traceur. Trois niveaux de mesure : lectures pures (WS, RPC, funding : gratuites) ; transactions traceuses Aave (supply ~10 USDC, borrow 1, repay, withdraw, gas en centimes, à heures variées y compris les fenêtres volatiles) ; traversées traceuses du pont (5-10 USDC, un aller-retour par jour minimum, frais ~1 $ par retour) et ordres HL post-only de taille minimale loin du prix, posés puis annulés. L'executor expose donc un mode TRACER avec une liste blanche de ces micro-opérations. Pendant M1, le moteur de décision tourne sur données réelles et journalise les actions qu'il aurait prises (journal des tirs à blanc, relu en revue M1). Les mesures de latence continuent indéfiniment en production.

Précision M3 : le plancher de capital du LIVE_SMALL est ~1 500-2 000 $. En dessous, les minima de plateforme (10 $ de notionnel, 5 $ de dépôt) et les frais fixes dénaturent le test : on ne teste plus la machine, on teste les effets de seuil. Option de rampe : première semaine de M3 en châssis dégradé (exposition 1,5x), seconde semaine en châssis complet.

---

## 15. Tests

Vue d'ensemble, du plus abstrait au plus réel :

| # | Test | Ce qu'il valide | Étape |
|---|---|---|---|
| 0 | Recette lecture seule | connexions et snapshot | M0 |
| 1 | Unitaires decision.py | la logique pure | M2 |
| 2 | Rejeu court (30 j) | les déclenchements sur données récentes | M2 |
| 3 | Backtest long (~5 ans) | les paramètres à travers les régimes | M2b |
| 4 | Intégration (testnet HL + fork Arbitrum) | l'exécution : fills partiels, reverts, RPC coupé | M2-M3 |
| 5 | Chaos | la survie du bot à son propre crash | M2-M3 |
| 6 | Marche à blanc 7 j (TRACER) | l'infrastructure réelle : les latences | M1 |
| 7 | LIVE_SMALL 2 semaines | l'exécution en conditions réelles | M3 |
| 8 | Revue de bascule | la décision de passer en vrai | M4 |

### 15.1 Unitaires
100 % de `decision.py` et du solveur d'état cible. Table de cas explicite : chaque ligne de la table de décision, chaque bord de seuil (0.0349 vs 0.0351 pour la réduction, 0.789 vs 0.791 pour le coussin), chaque combinaison de priorités concurrentes (la plus prioritaire gagne), les seuils asymétriques (+0.044 : rien ; +0.046 : re-centrage ; -0.059 : rien ; -0.061 : re-centrage).

### 15.2 Rejeu court (M2)
30 jours récents, prix + funding horaires réels. Vérifie que les déclenchements tombent aux bons moments et qu'aucune action interdite n'est émise. Objectif : exactitude de la décision, pas performance.

### 15.3 Backtest long (M2b)
Objectif : tester les PARAMÈTRES (bandes, seuils, porte de régime) à travers tous les régimes connus, pas prédire le rendement futur.

Période cible : ~5 ans, en deux segments de fidélité documentés :
- Segment fidèle (mi-2023 -> aujourd'hui) : funding horaire Hyperliquid réel (archives publiques API/S3).
- Segment proxy (2021 -> mi-2023) : funding Binance ETHUSDT (8 h, réparti en pas horaires), étiqueté PROXY dans les rapports. Hyperliquid n'existait pas : ce segment teste les règles, pas la venue.

Données :
- Prix ETH : bougies horaires (API publique Binance ou équivalent) sur toute la période, PLUS fenêtres 1 minute sur les épisodes de stress (mai 2021, juin 2022 avec depeg stETH, 10 octobre 2025, février 2026).
- Taux d'emprunt USDC : Aave v3 Arbitrum depuis mars 2022 (subgraph/Aavescan) ; avant : proxy Aave v2 mainnet, étiqueté PROXY.
- Staking : historique APR Lido. Ratio stETH/ETH historique pour le stress de depeg.

Méthode :
- Les bandes se testent contre les HIGH/LOW des bougies, jamais contre les clôtures (une mèche liquide aussi bien qu'une clôture).
- Latence de pompe simulée = p95 mesuré en M1 (couplage M1 -> M2b) ; pendant la fenêtre de latence, le prix continue de courir sur les données 1 minute.
- Frais, slippage et coûts de pont appliqués à chaque opération simulée.

Sorties attendues :
- Nombre de liquidations simulées (cible : zéro avec pompe p95 ; recensement des cas limites).
- Re-centrages par an et par sens, coût annuel des frais.
- Chronologie des régimes : mois OUVERT / INTERMÉDIAIRE / PARKED, et durée du plus long PARKED.
- Croissance recomposition simulée vs classeur à hypothèses égales (écart < 2 %).
- A/B : porte de régime ON vs OFF sur la période entière (la valeur de la porte en points de rendement et en accidents évités).

### 15.4 Intégration et chaos
Testnet Hyperliquid pour la jambe perp ; fork Arbitrum (anvil) pour la jambe Aave. Scénarios : fill partiel, tx revert, RPC coupé en pleine opération (le bot doit finir ou marquer `failed` proprement, jamais d'état intermédiaire silencieux). Chaos : couper le WS pendant un re-centrage, tuer le process entre `pending` et `sent`, simuler un pont à 30 min. Vérifier la réconciliation au redémarrage.

---

## 16. Runbook opérateur

- Démarrer : `python -m src.main --config config.yaml`. Le bot démarre toujours en réconciliation, puis reprend l'état persistant.
- Pause propre : créer le fichier `KILL`. Reprise : le supprimer puis commande `resume`.
- Dégonflage d'urgence manuel : `KILL_DEFLATE` ou commande Telegram `deflate`.
- Intervention manuelle sur les positions : mettre en pause d'abord, TOUJOURS. Le bot réconciliera au resume.
- Chaque CRITICAL exige un post-mortem écrit dans `incidents/` avant tout retour en LIVE.

---

## 17. Décisions figées et non-objectifs v1

Décisions figées (ne pas rouvrir pendant l'implémentation) :
1. Une seule venue perp (Hyperliquid), une seule chaîne (Arbitrum), une seule paire (ETH).
2. Pas de smart contract custom en v1 : boucle itérative à la construction, désendettement par étapes via le coussin (le flashloan one-shot est une optimisation v2).
3. Levier short fixe 10x, LTV cible 70 %, exposition dérivée (2,5x) ; l'exposition n'est pilotée que par la porte de régime.
4. Politique d'écrémage v1 : recomposition quand la porte est OUVERTE, désendettement sinon. Le bot ne verse jamais de dividende de sa propre initiative.
5. Le bot ne modifie jamais ses propres seuils ; tout changement de config exige un redémarrage explicite.
6. Clés sur le serveur du bot, capital plafonné en conséquence.

Non-objectifs v1 : multi-venue (y compris Lighter, réévalué seulement en cas de campagne de points confirmée), routage de funding, LRT en collatéral, interface web, optimisation fiscale, toute forme de prise de position directionnelle.

---

## 18. Références à consulter pendant l'implémentation

- Documentation API et SDK Hyperliquid (ordres, marge isolée, funding history, retraits, agent wallets, maintenance margin, archives de données historiques).
- Documentation Aave v3 (Pool, DataProvider, adresses Arbitrum, e-mode, subgraph pour l'historique des taux).
- Documentation du pont Hyperliquid (Bridge2, délais, minimums).
- Sources de données backtest : API publiques de bougies et de funding (Binance), archives Hyperliquid, Aavescan/subgraph Aave, historique APR Lido.
- Vérifier à jour de l'implémentation : paramètres de risque wstETH/USDC sur Aave Arbitrum, frais HL, minimums du pont. Toute valeur codée en dur qui peut être lue on-chain ou via API doit être lue, pas codée.
