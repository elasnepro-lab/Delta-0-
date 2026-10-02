# Audit finance — 2026-10-02 — commit ce63d79 (main)

Revue `/revue-finance`, à contexte frais : la session qui a produit ce rapport n'a ni écrit ni modifié le code audité. Méthode : trois sous-agents à contexte frais, en lecture seule et en parallèle :
- **A** : rapprochement spec → code → test ;
- **B** : unités et conventions ;
- **C** : paramètres réels et scénarios.

Consolidation par la session principale. Aucune correction de code pendant la revue.

**Verdict : rapprochement NON SOLDÉ.** 13 écarts MAJEURS, dont 4 portent un désaccord à trancher (§4), 22 MINEURS et 6 questions de spec pour l'opérateur. Les écarts MAJEURS bloquent le passage LIVE_SMALL.

**Ce qui tient.** La conversion wstETH/ETH, l'erreur historique de 24,5 %, est juste : le ratio est lu à l'oracle Aave et rafraîchi à chaque cycle, en production comme en backtest. Les bandes du flanc bas sont justes à 0,03 pt près. Le moteur `decide()` reproduit au dollar près les calculs faits à la main sur les trois scénarios.

**Ce qui ne tient pas.** L'essentiel du risque est côté Hyperliquid :
- la réserve libre compte probablement deux fois la marge isolée ;
- la moyenne de funding sur « 30 j » lit les 20,8 jours les plus anciens ;
- la maintenance margin n'est comparée à rien ;
- la défense du flanc haut a été conçue pour une boucle de production qui n'existe pas encore : pas de préemption, pas de gel, pas de REPAIRING.

---

## 1. Référentiel 1 : valeurs réelles lues pendant l'audit

Lecture le 2026-10-02 vers 03:53 (heure de Paris). Aucune valeur n'est citée de mémoire.

| Paramètre | Valeur | Source de la lecture |
|---|---|---|
| wstETH Aave v3 Arbitrum : décimales, LTV max, LT, bonus | 18 ; 0,75 ; **0,79** ; 1,072 (pénalité 7,2 %) | `scripts/read_aave_params.py`, bloc 510843519 |
| wstETH : gel, pause, caps | actif, non gelé ; supply cap 34 000 (19 197 déposés) | idem |
| USDC natif : décimales, LTV, LT, bonus | 6 ; 0,75 ; **0,78** ; 1,05 | idem |
| E-mode | aucune catégorie éligible pour une dette USDC contre du wstETH → e-mode 0 | idem |
| Oracle Aave | wstETH 3 377,32 $ ; WETH 2 712,00 $ ; USDC 0,99998 $ ; ratio **1,245326** | idem (`stEthPerToken` absent sur Arbitrum) |
| Taux d'emprunt variable USDC | **3,878 %/an** (APR) ; supply 3,046 % | `getReserveData` via RPC public arb1, bloc 510844646 (agent C) |
| HL ETH : szDecimals, maxLeverage | 4 ; 25 (table 55, « tiered 25x » : 15x au-delà de 100 M$) | API `info` `metaAndAssetCtxs` |
| HL maintenance margin ETH | 2 % = 1/(2·25) | voir désaccord D2 sur le niveau de preuve |
| HL frais du compte maître | taker 4,5 bps ; maker 1,5 bps ; remise de parrainage 4 % active | API `info` `userFees` |
| HL mark / oracle / mid | 2 715,9 / 2 716,36 / 2 716,35 | `metaAndAssetCtxs` |
| HL funding ETH 30 j, paginé | 720 points du 02/09 02:00 au 02/10 01:00 UTC ; **10,19 %/an** (×8760) ; 30 heures négatives | `fundingHistory` paginé |
| HL funding 30 j, non paginé (ce que lit le bot) | 500 points du 02/09 02:00 au **22/09 21:00** ; 9,75 %/an | `fundingHistory` en un appel |

Désaccord avec le référentiel 2 ou 3 : `config.yaml.example` et `scripts/classeur.py` fixent la cible LTV à **0,675**. La mémoire de projet, non référentielle, dit que le backtest a retenu 0,625 (observation O1).

## 2. Bilan de référence (`scripts/classeur.py`, config de l'exemple, LT 0,79)

Point fixe solveur ↔ classeur : écart 0,0000 % (exécuté). Tout est linéaire en capital.

| Poste | 20 000 $ | 100 000 $ |
|---|---|---|
| Coussin (5 %) | 1 000 | 5 000 |
| Spot wstETH | 41 758 | 208 791 |
| Dette USDC (0,675 × spot) | 28 187 | 140 934 |
| Marge isolée HL (10 %) | 4 176 | 20 879 |
| Réserve libre HL (3 %) | 1 253 | 6 264 |
| Short | 15,398 ETH | 76,988 ETH |
| LTV Aave observée / HF | 0,6592 / 1,198 | idem |
| Exposition spot / équité (déf. README §4) | 2,09x | idem |

### Bandes du flanc bas (baisse de prix depuis la cible)

La colonne « réel » prend le coussin au LT 0,78, avec un LT pondéré du compte de 0,78977.

| Seuil | LTV | Coussin plein, classeur | Coussin plein, réel | Coussin vide |
|---|---|---|---|---|
| P6 pompe | 0,750 | −12,39 % | −12,36 % | −10,00 % |
| P3 coussin | 0,765 | −14,16 % | −14,13 % | −11,76 % |
| P4 désendettement | 0,775 | −15,30 % | −15,27 % | −12,90 % |
| Liquidation | 0,790 | −16,95 % | **−16,92 %** | −14,56 % |

### Bandes du flanc haut (calculées par la revue ; ni le README ni le classeur ne les publient)

Hypothèse : MM 2 % et `marginUsed` incluant le PnL latent.

| Seuil | Marge / notionnel | Hausse de prix |
|---|---|---|
| I3 croisière | 0,07 | +2,80 % |
| P7 re-centrage haut | — | +4,50 % |
| P5 pompe montante | 0,05 | +4,76 % |
| P2 urgence | 0,035 | +6,28 % |
| Liquidation HL | 0,02 | **+7,84 %** |
| Liquidation après versement de la réserve par P2 | | +10,78 % |

## 3. Scénarios rejoués (à la main, puis par `decide()` : concordance totale)

| Scénario | Aave | HL | Décision du code | Montants (20 k / 100 k) |
|---|---|---|---|---|
| **ETH −15 %**, choc instantané | LTV 0,7724, HF 1,0225 ; reste −2,26 % avant liquidation | ratio 0,294, rien | P3 REPAY_FROM_CUSHION, ×4 cycles, puis P6 PUMP_DOWN | 250 $ par tranche (1 250 $) ; P6 2 873 $ (14 365 $) |
| **ETH +8 %**, choc instantané | LTV 0,611, HF 1,29 | ratio 0,0185 < MM : **short liquidé** (+8 % > +7,84 %) | P2 émis trop tard | perte ≈ 900 $ (4 500 $) + jambe longue nue de 41,8 k (209 k) jusqu'à P1 |
| ETH +8 %, chemin graduel | — | P7 à +4,6 %, P5 à +5 %, P2 à +6,5 % | position survivante, liquidation repoussée à +10,78 % | P5 2 297 $ (11 484 $) ; P2 1 253 $ (6 264 $) |
| **Décrochage du marché stETH −5 %** | oracle Arbitrum insensible au marché stETH (`docs/backtest/verification-oracle-wsteth.md`) : snapshot inchangé | inchangé | NOOP | perte latente de 2 088 $ (10 440 $), réalisée à la vente ; sur-couverture ≈ 5 % |
| Variante : slashing Lido −5 % (taux de change) | LTV 0,693, HF 1,14 | — | P8 RETRUE_SHORT vers 14,63 ETH (73,14) | perte non couverte 2 088 $ (10 440 $) |

---

## 4. Désaccords entre agents (règle 4 : remontés côte à côte, pas votés)

**D1. La sémantique de `marginUsed` : inclut-il le PnL latent ?** Tout le flanc haut en dépend (P2, P5, I3).
- **A et C** : NON VÉRIFIÉE. Le point A7 de `memory/hl_findings.md` est resté ouvert, et la mesure du projet (0,007 $ de PnL) ne permet pas de trancher. Si le PnL est exclu, P2 et P5 ne tirent jamais avant la liquidation à +7,84 %. Perte : la marge entière, 4 176 $ (20 879 $), plus une jambe nue de 41,8 k (209 k). Gravité MAJEUR.
- **B** : VÉRIFIÉE le 02/10 sur des positions tierces isolées de mainnet. Sur un short ETH, `rawUsd − szi·mark` = 11,76 contre `marginUsed` 11,78 ; sur BTC, 17,7079 contre 17,7079. Donc `marginUsed` est l'équité isolée, PnL compris. Pas d'écart.
- Lecture de la consolidation : la mesure de B est directe et discriminante, mais elle porte sur des comptes tiers. **Proposition** : considérer le point probablement soldé, le confirmer sur notre compte à la première position, et ajouter un déclencheur sur la distance à `liquidationPx`, robuste aux deux lectures.

**D2. Le niveau de preuve de la maintenance margin à 2 %.**
- **A et C** : NON VÉRIFIÉE, déduite de la formule de la doc HL.
- **B** : VÉRIFIÉE par le `liquidationPx` publié d'un short tiers à 25x : 429,713 / (0,1538 × 1,02) = 2 739,19, identique.
- La valeur est la même dans les deux lectures. Seule la preuve diffère, et elle ne vaut que pour un compte tiers.

**D3. La gravité de l'absence de contrôle de la MM (écart F5).** C : MAJEUR. A et B : MINEUR, parce que le trou est documenté et sans impact aujourd'hui. La consolidation retient **MAJEUR** : une baisse de `maxLeverage` à 15 décidée par la gouvernance HL ramènerait l'écart entre P2 et la liquidation à 0,17 pt, sans aucune alerte (table en F5).

**D4. La gravité de la cible de remboursement de P6 et P4 (écart F7).** B et C : MAJEUR. A : MINEUR, montant non spécifié. Le fond relève de la spec : à trancher par l'opérateur.

---

## 5. Écarts chiffrés

Format : spec / code / réel, puis impact sur 20 k$ et 100 k$. Les preuves sont en `fichier:ligne` au commit ce63d79.

### MAJEURS

**F1. La réserve libre HL lit `total`, qui inclut la marge isolée engagée (compte unifié).** Agents B et A.
- **Spec** : README §2, §5 et §9.1 : la réserve est l'USDC *non engagé* comme marge.
- **Code** : `venues/hyperliquid.py:184-187` renvoie `entry["total"]` et ignore `hold`.
- **Réel** : sur des comptes tiers unifiés avec une position isolée, `total` inclut la marge, `hold` ≈ `marginUsed`, et le disponible vaut `total − hold` (= `tokenToAvailableAfterMaintenance`). B l'a mesuré ce jour. Ce n'est pas vérifié sur notre compte, faute de position ouverte. La mesure mainnet du 09/09 (`hl_findings` §16) va dans le même sens.
- **Impact** :
  - Équité surestimée de la marge : +4 176 $ (+20 879 $). Le solveur vise alors un spot de 50 936 $ (254 679 $), soit +22 % ; dette cible +6 195 $ (+30 975 $) ; exposition réelle 2,55x au lieu de 2,09x ; liquidation à coussin plein −16,52 % au lieu de −16,95 %.
  - P2 croit disposer de 5 429 $ (27 143 $) au lieu de 1 253 $ (6 264 $). Il demande environ 2 885 $ (14 425 $), probablement refusés par HL (NON VÉRIFIÉ), et préfère ADD au repli REDUCE. La seule défense rapide du flanc haut tombe : **−2,94 pt de bande**, liquidation à +7,84 % au lieu de +10,78 %.
  - P10 et I4 sont faussés de la même façon.
- **Tests** : aucun avec `hold` différent de 0.

**F2. La moyenne de funding sur « 30 j » n'est pas paginée et lit la fenêtre la plus ancienne.** Agents A, B et C, vérifié.
- **Spec** : README §5 l.154, moyenne sur 720 h × 8760.
- **Code** : `venues/hyperliquid.py:124-142`, un seul appel. La docstring dit « last 720 samples », c'est faux.
- **Réel** : 500 points du 02/09 au 22/09, **9,2 jours de retard** ; 9,75 %/an contre 10,19 % paginé.
- **Impact** : avec les 7 jours d'hystérésis, la porte de régime voit le funding avec environ 16 jours de retard. Une bascule à tort entre exposition pleine et moitié déplace 14 485 $ (72 427 $) de spot. Le pire cas est une sortie tardive vers PARKED après un passage du funding sous zéro.
- Le backtest, lui, pagine (`backtest/hl_funding.py:141-168`) : la production et le backtest ne mesurent pas la même chose.
- Aujourd'hui la porte n'est pas câblée et l'écart reste latent, mais il **bloque le chantier 8.6**.
- **Tests** : aucun.

**F3. Le signe de la position HL est perdu.** Agent B.
- **Code** : `watcher.py:126` calcule `short_size = abs(size_signed)`.
- **Impact** : un long, après un fill erroné ou une action manuelle, est lu comme un short. Le delta apparaît nul alors que l'exposition réelle est d'environ +83,5 k$ (+417,6 k$). P8 et I1 restent muets.
- Probabilité faible, conséquence non bornée. Correctif trivial.

**F4. La préemption des urgences (§6) n'existe pas dans `src`, et le backtest la désactive par défaut.** Agent A.
- **Code** : `backtest/engine.py:208` fixe `preempt=False`.
- **Impact** : un re-centrage de 316 s bloque P2, dont le budget est de 2 s. Selon la mémoire du projet (non ré-exécuté), 8 liquidations sur 8 sans préemption le 2024-05-20. Les campagnes de backtest lancées avec les valeurs par défaut testent un bot non conforme au README.

**F5. La maintenance margin n'est ni lue directement ni comparée, et les seuils HL ne sont pas dérivés d'elle.** Agent C (A et B en MINEUR, voir D3).
- **Code** : `venues/hyperliquid.py:86-92` calcule 1/(2·maxLev) sans s'en servir. `config.maintenance_margin` (`schema.py:293`) n'est lue par aucun code. Le contrôle `margin_ratio_reduce > MM` est absent, alors que le README l.112 et le §9.1 l'exigent. Aucune alerte si `maxLeverage` change.
- **Sensibilité** :

| maxLeverage HL | MM | Liquidation | Écart entre P2 (+6,28 %) et la liquidation |
|---|---|---|---|
| 25 (aujourd'hui) | 2 % | +7,84 % | 1,56 pt |
| 20 | 2,5 % | +7,32 % | 1,04 pt |
| 15 | 3,33 % | +6,45 % | **0,17 pt** |
| < 14,3 | ≥ 3,5 % | — | P2 ne tire jamais avant la liquidation |

**F6. Le gel après BLIND, l'état REPAIRING et le verrou de re-tir de P2 sont absents de `src`.** Agent A, effet inféré faute de boucle de production.
- **Code** : `decision.py:293-321` et `:369-399` ré-émettent les actions à chaque cycle.
- **Impact** :
  - BLIND HL_ONLY : réduction de 50 % à chaque cycle de 5 s. Après 4 cycles, il reste 6,25 % du short, soit une jambe nue de 39 147 $ (195 742 $). Une baisse de 10 % coûte alors 3,9 k$ (19,6 k$).
  - Repli P2 : −30 % par cycle.
  - BLIND AAVE_ONLY : vide le coussin en 4 cycles avec un HF sain.
- Le verrou n'existe que dans `tests/simulator.py`.

**F7. P6 et P4 remboursent jusqu'à `target_ltv + 0,01` en LTV Aave (coussin compris), alors que `target_ltv` est défini sur le spot seul.** Agents B, C et A (voir D4).
- **Code** : `decision.py:463-465` et `:436`.
- **Spec ambiguë** : le glossaire l.36 et le §8.7 prennent la LTV Aave ; le §3 l.99 et le §11 l.328 prennent la dette sur le spot.
- **Impact au déclenchement de P6, coussin plein** :
  - le code rembourse 2 434 à 2 473 $ (12 169 à 12 365 $) et laisse une dette sur spot de 0,7037 ;
  - une cible de 0,685 × spot ferait rembourser 3 119 à 3 158 $ (15 594 à 15 790 $) ;
  - écart ≈ **685 $ (3 425 $), soit −2,4 à −2,5 pt de bande** jusqu'à la pompe suivante et jusqu'à la liquidation.
- La même ambiguïté touche I2 en croisière (m4).

**F8. La gouvernance Aave n'est contrôlée qu'au boot, et la condition de contrôle est trop lâche.** Agents C et A.
- Le LT est bien relu à chaque cycle (`watcher.py:142`). Mais :
  - `bands_incoherence` (`decision.py:260-276`) n'exige que pompe > cible. Pour un LT entre 0,715 et 0,725, la cible de remboursement de P6 (0,685) reste au-dessus du seuil de pompe : P6 tourne en boucle.
  - Le contrôle n'existe qu'au boot (`reconcile.py:57`). Rejeu : un LT coupé à 0,69 en cours de route donne un PUMP_DOWN de **0 $ à chaque cycle, qui bloque P7 à P10 indéfiniment**.
  - Un LT lu à 0 en cours de route rend muettes toutes les défenses basses.
  - L'ordre re-centrage avant pompe s'inverse sous un LT de 0,740 (coussin plein) ou de 0,758 (coussin vide), alors que le boot ne refuse qu'à partir de 0,715.

| LT | Liquidation (coussin plein / vide) | Pompe (coussin vide) | Boot |
|---|---|---|---|
| 0,79 | −16,95 / −14,56 | −10,00 | OK |
| 0,77 | −14,73 / −12,34 | −7,53 | OK |
| 0,75 | −12,39 / −10,00 | −4,93 (avant le re-centrage à −6 %) | OK |
| 0,73 | −9,93 / −7,53 | −2,17 | OK |

- Le seuil de désendettement absolu à 0,775 n'existe plus : il est dérivé, LT − 0,015. C'est conforme.

**F9. I2 « jamais » contredit les conditions de P3 et P4.** Agent A, contradiction interne de la spec ; le code suit la spec.
- Quand le coussin est inférieur à la tranche et que la LTV est entre 0,765 et 0,775 (de −11,76 % à −12,90 %), ni P3 ni P4 ne tirent. Seul P6 tire, et I2 ne le compte pas (`invariants.py:60-63, 160`). Son p95 de 316 s dépasse la grâce de 300 s.
- I2 passe alors en CRITICAL et **dégonfle** (`invariants.py:285-295`) une situation que la table de décision traite normalement : 41,7 k (208,8 k) de position sortie, coûts de sortie réalisés, carry arrêté.
- Arbitrage pour l'opérateur : compter P6 comme défense, ou faire tirer P4 dès que le coussin est vide.

**F10. P10 n'a pas de limite d'une tranche par heure, et sa convergence est géométrique.** Agent A.
- **Spec** : §8.9 l.264.
- **Code** : `decision.py:551-552`, 25 % de l'écart restant, sans horloge, avec une zone morte de 1e-6.
- **Calcul** : 4 tranches ne font que 68,4 % du chemin ; il faut 48 pas pour converger.
- Le backtest a mesuré, sans garde-fou, 19 913 tirs et 20 384 $ de frais en 4 mois sur 20 k$ (chiffre cité, non ré-exécuté). Ordre de grandeur ×5 sur 100 k$.

**F11. Les provisions du classeur sont en dollars fixes, et aucune ne couvre une liquidation HL.** Agents C et A.
- **Code** : `scripts/classeur.py:41-50`. Les 1 200 $ attendus se décomposent en 1 000 + 100 + 100, sans dérivation et sans lien avec `backtest/costs.py`. Le total prudent est de 3 320 $, alors que le README §12 l.355 annonce 3 480 $ par an (40 + 250 $ par mois), un chiffre dérivé écrit dans le README contre sa propre règle.
- **Impact** :
  - à 100 k$, le classeur affiche une année prudente à **18,2 %** ; avec des provisions proportionnelles, elle tombe à **3,9 %** ;
  - la provision « liquidation Aave » de 1 300 $ se compare à 1 015 $ à 20 k$ mais à 5 074 $ à 100 k$ (close factor de 50 % NON VÉRIFIÉ) ;
  - aucune ligne pour une liquidation HL (≈ 900 $ / 4 500 $), alors que c'est le flanc qui liquide dans le backtest.

**F12. La détection Aave `LiquidationCall` pour P1 n'est pas câblée.** Agent A. `tracer.py:320-321` la marque « M2 concern ». Après une liquidation Aave, le short n'est pas recalé : l'ordre de grandeur est la part liquidée du spot (close factor NON VÉRIFIÉ), qui reste sur-couverte.

**F13. La table de décision est conçue pour un bot qui exécute, mais aucun exécuteur n'existe pour P3 à P10.** Agent A, trous documentés.
- Il manque :
  - l'évaluateur de régime quotidien ;
  - les sous-étapes 3a, 3b, 4 et 5 de SKIM ;
  - les procédures 8.1 à 8.4 et 8.7 ;
  - l'application du mode prudent à `decide()` ;
  - le plafond LIVE_SMALL ;
  - la vérification du levier 10x au boot ;
  - le HF local de contrôle.
- Gravité MAJEUR au regard de LIVE_SMALL uniquement. Ce sont des chantiers du plan, pas des erreurs de formule.

### MINEURS

| # | Écart | Preuve | Impact (20 k / 100 k) |
|---|---|---|---|
| m1 | Le classeur et le backtest appliquent le LT 0,79 au coussin USDC (réel 0,78) | `classeur.py:81,154-155`, `backtest/ledger.py:167-172` | bandes optimistes de 0,03 pt (13 $ / 63 $) ; le moteur est juste |
| m2 | Le libellé « coussin vide » décrit un coussin parti sans dette remboursée ; « bande basse réelle » ne dit pas « coussin vide » | `classeur.py:12-14,194-195`, `read_aave_params.py:308` | le chiffre est conservateur, le libellé est faux |
| m3 | Le classeur affiche une exposition de 2,14x (collatéral / capital) ; le README §4 la définit comme spot / équité, soit 2,09x | `classeur.py:74-76` | documentation |
| m4 | I2 en croisière compare la LTV Aave, coussin compris, à `target_ltv` + 0,02 | `invariants.py:149-154` | WARN à −5,27 % au lieu de −2,88 % ; même question de spec que F7 |
| m5 | Planchers de croisière I2 et I3 à l'intérieur des bandes de re-centrage | `invariants.py` | WARN en croisière normale à −5,27 % et +2,80 % (alertes parasites) |
| m6 | `mark_price` est en fait le mid (`all_mids`), alors que HL liquide au mark | `hyperliquid.py:72-77`, `hl_stream.py:71,122-137` | ≤ 1,3 bp au repos ; ≈ 0,02 pt sur le ratio en stress |
| m7 | Seulement 0,26 pt entre le re-centrage haut (+4,50 %) et P5 (+4,76 %) | config | un mark bruité peut faire tirer la pompe (3 min) avec le re-centrage |
| m8 | Le flanc haut n'est pas publié par le classeur (reconnu par le README) | `classeur.py` | les chiffres sont au §2 ci-dessus |
| m9 | Hypothèses de carry du classeur contre les valeurs réelles : funding 11 % (réel 10,19 %), emprunt 5 % (réel 3,878 %), staking 2,7 % (≈ 2,23 %, inféré) | `classeur.py:35-37` | brut 4 094 $ (20,5 %) contre 4 312 $ (21,6 %) ; 20 469 $ contre 21 558 $ à 100 k ; au funding de 5,25 % : brut 10,2 %, année typique 4,2 % |
| m10 | P9 : excédent mesuré contre notionnel × 0,10 et non contre `margin_target` ; déclenche à « ≥ » ; premier écrémage immédiat si `last_skim_at` vaut None | `decision.py:526,580-615` | quelques dollars ; le README se contredit sur > ou ≥ |
| m11 | Constantes métier en dur. `_HF_ALERT_FLOOR = 1.10` porte un commentaire faux : « ~5 pt » alors que c'est 7,2 pt de LTV, soit −8,4 % | `schema.py:56`, `reconcile.py:28,30,103`, `decision.py:436,463` | contraire au README §4 |
| m12 | La LTV max (0,75) est lue mais n'est utilisée par rien ; `ltv_pump` = 0,75 exactement, par coïncidence | `watcher.py:143` | si la LTV max descendait à 0,675, P5 serait plafonné à 2 084 $ pour 2 297 $ demandés, et l'échec n'apparaîtrait qu'à l'exécution |
| m13 | L'oracle Aave est résolu une fois par processus ; `_BASE_DECIMALS = 10**8` est en dur pour le compte | `aave.py:404-424` | un changement d'oracle par la gouvernance est ignoré jusqu'au redémarrage |
| m14 | Le garde-fou TRACER estime le wstETH à 3 000 $ « conservative » alors qu'il vaut 3 377 $ | `executor.py:582-585` | sous-estimation de 11,2 %, non conservatrice |
| m15 | `round_size` arrondit au plus proche, sans direction | `hl_executor.py:92-94` | ≤ 0,14 $ ; à rendre directionnel en phase 8 |
| m16 | `costs.py` : frais HL 4,5 / 1,5 bps confirmés ce jour mais toujours marqués non vérifiés ; remise de 4 % non prise en compte ; frais de pont appliqués dans les deux sens | `backtest/costs.py:116-127` | ≈ 0,2 bp de trop, côté prudent |
| m17 | Le commentaire de `types.py` dit qu'un décrochage stETH « déplace la LTV », ce que la vérification de l'oracle contredit | `types.py:131-133` | documentation |
| m18 | Le glossaire du README §2 définit le delta en USD, le §5 et le code en ETH | README | documentation |
| m19 | P4 exige un coussin inférieur à la tranche : jusqu'à 249 $ (1 249 $) de coussin restent inutilisés | `decision.py:420-437` | marginal |
| m20 | Docstring `OperationalContext` périmée (« spot / equity ») | `decision.py:45` | documentation |
| m21 | Validateurs et lecteurs financiers sans test : `_check_ltv_below_liquidation`, `exposure_mult_half < exposure_mult`, `read_free_usdc`, pagination du funding | — | trous de preuve |
| m22 | `config.yaml` local (non versionné) : commentaires de bandes périmés (« depuis LTV 0,70 », « +8 % band ») | `config.yaml` | aucun sur le code |

## 6. Questions de spec pour l'opérateur (aucune n'a été tranchée par la revue)

- **O1. Cible LTV.** README §17 et `config.yaml.example` : 0,675. Mémoire du backtest : 0,625 retenue (à 0,675, « 4 morts / 1 survie »). À figer avant LIVE_SMALL ; toutes les bandes ci-dessus sont calculées à 0,675.
- **O2. Base de la porte de régime.** Elle mesure funding − emprunt, pas le carry réel. Le seuil de rentabilité du jour est f* ≈ 0,675 × 3,88 − 2,23 ≈ 0,39 %, mais la règle met le montage en PARKED dès f < 3,88 %. Au funding actuel (spread 6,31 %), l'exposition pleine est correcte. Le conservatisme n'est justifié nulle part. La porte compare aussi une moyenne sur 30 jours à un taux d'emprunt instantané.
- **O3. Faisabilité de P4 sans flashloan.** Passer d'une LTV de 0,775 à 0,685 en gardant HF ≥ 1,01 demande 15 itérations (16 à 100 k$), soit environ 45 transactions, pour un budget de 60 s. Ce chemin n'a jamais été mesuré.
- **O4. P1 après une liquidation HL.** « short = spot_eth » ré-ouvre un short entier. Faut-il couper ou recaler ?
- **O5. Décrochage combiné à un krach.** Si le garde-fou de slippage de 30 bps (§9.4) est mesuré contre l'oracle, chaque swap de P4 est refusé pendant un décrochage du marché stETH (cas de juin 2022). Le module de swap n'est pas écrit : point à cadrer avant de l'écrire.
- **O6. Arbitrages F7 et F9**, ci-dessus.

## 7. Valeurs NON VÉRIFIÉES restantes

Barème du backtest, sortie de `backtest.costs.DEFAULT.unverified()` au commit audité :

| Paramètre | Valeur | État à l'issue de l'audit |
|---|---|---|
| `bridge_fee` | 4 bps | NON VÉRIFIÉE |
| `bridge_fixed` | 1 $ | NON VÉRIFIÉE (le README §9.1 ne cite que 1 USDC au retrait) |
| `bridge_gas` | 150 000 gas | NON VÉRIFIÉE |
| `gas_price` | 0,01 gwei | NON VÉRIFIÉE |
| `hl_maker_fee` | 1,5 bps | **vérifiée ce jour** (`userFees`) ; remise de 4 % : inclusion NON VÉRIFIÉE |
| `hl_taker_fee` | 4,5 bps | **vérifiée ce jour** (`userFees`), même réserve |
| `swap_fee` | 5 bps | NON VÉRIFIÉE |
| `swap_gas` | 250 000 gas | NON VÉRIFIÉE |
| `swap_slippage` | 5 bps | NON VÉRIFIÉE |

Hors barème :
- Maintenance margin ETH à 2 % sur NOTRE compte (D2).
- Sémantique de `marginUsed` sur NOTRE compte (D1).
- `total` et `hold` sur NOTRE compte avec une position ouverte (F1).
- Réponse de HL à un `update_isolated_margin` supérieur au disponible.
- Minimum d'ordre HL de 10 $ (aucun endpoint).
- Close factor Aave de 50 %.
- Règle « LTV 0 » d'Aave sur un retrait de wstETH en P4.
- Rendement de staking wstETH (≈ 2,23 %/an, inféré de deux lectures du ratio : 1,2440996 le 15/09, 1,245326 le 02/10).
- Comportement de HL sur une position ouverte si `maxLeverage` baisse.

## 8. Table de rapprochement complète : règle → code → test (agent A)

Légende : ✅ soldé · ⚠️ écart (renvoi) · ❌ sans code · 🔶 sans test.

| # | Règle (README) | Code | Test | Statut |
|---|---|---|---|---|
| R1 | §3 spot = m(E−c)/(1+m·r) | `decision.py:115-123` | `test_target_state.py:test_reference_balance_matches_the_classeur`, `test_the_reference_balance_sheet_is_a_fixed_point`, `test_properties.py:test_the_solver_is_a_fixed_point_for_any_equity` | ✅ |
| R2 | §3 notional_target = spot_target | `decision.py:124` | `test_solver_invariants` | ✅ |
| R3 | §3 margin_target = spot·target_margin_ratio | `decision.py:125` | `test_solver_invariants` | ✅ |
| R4 | §3 reserve_target = spot·hl_reserve_pct | `decision.py:126` | `test_solver_invariants`, `test_the_reserve_is_not_leveraged` | ✅ |
| R5 | §3 debt_target = target_ltv·spot | `decision.py:127` | `test_solver_invariants`, `test_the_resulting_ltv_sits_below_the_nominal_target` | ✅ |
| R6 | §3 le coussin sort de l'équité déployable | `decision.py:115-117` | `test_the_cushion_is_not_leveraged`, `test_solver_rejects_a_cushion_that_swallows_the_equity` | ✅ |
| R7 | §3 point fixe vérifié par le classeur | `classeur.py:221-231` | `test_the_solver_and_the_classeur_agree` | ✅ (0,0000 %) |
| R8 | §4 target_margin_ratio = 1/lev | `schema.py:348-356` | `test_target_margin_matches_leverage` | ✅ |
| R9 | §4 exposure_mult = 1/(1−tl+1/lev) | `schema.py:335-343` | `test_exposure_mult_matches_formula`, `test_reject_bad_exposure_mult` | ✅ |
| R10 | exposure_mult_half < exposure_mult | `schema.py:344-345` | — | 🔶 m21 |
| R11 | marges : pompe > coussin > désendettement | `schema.py:105-111` | `test_reject_ltv_margin_order`, `test_priorities_keep_their_order` | ✅ |
| R12 | seuil = LT − marge, comparé en HF = LT/(LT−m) | `decision.py:226-236,245-257` | `test_bands_derive_from_the_on_chain_threshold`, `test_every_band_leaves_room_before_liquidation` | ✅ |
| R13 | boot refusé si pompe ≤ target | `decision.py:260-276`, `reconcile.py:57-64` | `test_boot_refuses_when_the_pump_would_fire_at_rest`, `test_boot_refusal.py` | ✅ / ⚠️ F8 (condition trop lâche) |
| R14 | garde-fou statique sur la marge de pompe | `schema.py:385-402` | — | 🔶 |
| R15 | marge la plus serrée ≥ 0,01 sous le LT | `schema.py:56,112-117` | `test_reject_margin_too_close_to_liquidation` | ⚠️ m11 (constante sans règle) |
| R16 | margin_ratio_pump > margin_ratio_reduce | `schema.py:98-102` | `test_reject_reduce_above_pump` | ✅ |
| R17 | pompe et réduction au-dessus de la MM lue par l'API ; alerte en cas de divergence | — | — | ❌ F5 |
| R18 | LT et LTV max lus à chaque cycle | `watcher.py:142-143`, `aave.py:293-320` | `test_an_unreadable_threshold_raises_a_boot_refusal` | ✅ LT / ⚠️ m12 (LTV max inutilisée) |
| R19 | spot_usd au prix de l'oracle | `types.py:94-96`, `watcher.py:136` | `test_spot_notional_delta_zero_at_target` | ✅ |
| R20 | ratio wstETH/ETH = prix wstETH / prix WETH | `aave.py:225-234` | `test_delta_follows_the_staking_rate` | ✅ |
| R21 | collateral = spot + coussin ; ltv = dette/collateral | `types.py:102-114` | `test_ltv_matches_target`, `test_ltv_zero_when_no_collateral` | ✅ |
| R22 | HF lu on-chain pour P3, P4, P6 | `decision.py:404,422,460` | `test_p3_edge_*`, `test_p6_fires_at_ltv_pump` | ✅ déclencheurs / ⚠️ F7 (montants) |
| R23 | margin_ratio = marge isolée / notionnel | `types.py:120-124`, `hyperliquid.py:117` | `test_margin_ratio` (formule seule) | ⚠️ D1 |
| R24 | delta_pct en ETH | `types.py:126-146` | `test_delta_is_blind_to_price`, `test_p8_ignores_a_pure_price_move` | ✅ |
| R25 | price_move = (mark − ancre)/ancre | `decision.py:480` | `test_p7_*` | ✅ / ⚠️ m6 (mid) |
| R26 | funding_30d = moyenne sur 720 h × 8760 | `hyperliquid.py:124-142` | — | ⚠️ F2 |
| R27 | borrow_apr : ray → APR | `aave.py:339-340` | `test_aave_multicall.py` | ✅ |
| R28 | equity = collateral + marge + wallet + HL libre − dette | `types.py:173-190`, `hyperliquid.py:166-190` | `test_equity_reconstruction`, `test_free_balances_count_in_equity` | ⚠️ F1 |
| R29 | P1 : liquidation → short = spot restant | `decision.py:324-336`, `tracer.py:315-339` | `test_p1_liquidation_event_fires_first`, `test_blind_hl_only_still_honours_liquidation_event` | ⚠️ F12 (côté Aave absent), O4 |
| R30 | P2 : mr ≤ 0,035 | `decision.py:359-361` | `test_p2_edge_*` | ✅ |
| R31 | P2 : montant pour revenir à la cible, plafonné à la réserve | `decision.py:365-366` | `test_the_add_stops_at_the_nominal_ratio`, `test_the_reserve_is_spent_before_the_short_is_touched`, `test_p2_never_spends_more_than_the_reserve_nor_spends_it_in_vain` | ✅ formule / ⚠️ F1 (plafond) |
| R32 | P2 : réserve insuffisante → IOC reduce_fraction, CRITICAL, REPAIRING | `decision.py:369-399` | `test_a_reserve_too_small_to_clear_the_trigger_is_not_spent`, `test_an_empty_reserve_falls_back_to_closing` | ⚠️ F6 |
| R33 | §8.6.3 REPAIRING | — | — | ❌ F6 |
| R34 | P3 : HF ≤ seuil et coussin ≥ tranche | `decision.py:402-417` | `test_p3_edge_*`, `test_p3_takes_priority_over_p6` | ✅ |
| R35 | tranche = 25 % du coussin initial | `decision.py:279-287` | `test_p3_edge_at_threshold` | ✅ |
| R36 | P4 : HF ≤ seuil et coussin < tranche ; cible ≤ tl+0,01 | `decision.py:420-437` | `test_p4_fires_when_ltv_over_deleverage_and_cushion_empty`, `test_deleverage_fires_before_liquidation_when_the_cushion_is_gone` | ✅ / ⚠️ F7, O3 |
| R37 | P5 : mr ≤ 0,05 | `decision.py:440-455` | `test_p5_fires_below_pump_threshold`, `test_p2_takes_priority_over_p5` | ✅ déclencheur / montant non spécifié |
| R38 | P6 : HF ≤ seuil de pompe | `decision.py:458-474` | `test_p6_fires_at_ltv_pump` | ✅ déclencheur / ⚠️ F7 |
| R39 | P7 : +4,5 % / −6 % | `decision.py:477-501` | `test_p7_up_band_edge_*`, `test_p7_down_band_edge_*`, `test_p7_no_anchor_yields_no_recenter` | ✅ |
| R40 | P8 : abs(delta) > tolérance | `decision.py:504-519` | `test_p8_fires_when_the_hedged_quantity_drifts` | ✅ |
| R41 | P9 : créneau et excédent > skim_min | `decision.py:522-541,580-615` | `test_p9_*` | ⚠️ m10 |
| R42 | §8.5 SKIM 3a, 3b, 4, 5 | — (backtest seul) | — | ❌ F13 |
| R43 | §8.9 bandes de régime | `decision.py:178-184` | `test_le_regime_lit_le_spread_par_bandes` | ✅ / O2 |
| R44 | §8.9 hystérésis | `decision.py:187-199` | `test_la_porte_ne_bouge_pas_avant_la_confirmation` + 2 autres | ✅ |
| R45 | §8.9 évaluation quotidienne à 00:00 UTC | — (backtest seul) | — | ❌ F13 |
| R46 | §8.9 tranches de 25 %, une par heure au plus | `decision.py:544-564` | `test_p10_*` | ⚠️ F10 |
| R47 | exposition mesurée avec le capital immobilisé | `decision.py:138-155` | `test_exposure_mult_of_inverse_exactement_le_solveur` | ✅ / m20 |
| R48 | §6 les urgences préemptent tout | — ; backtest `preempt=False` | — | ❌ F4 |
| R49 | I7 : une seule opération à la fois | `invariants.py:235-244` | `test_i7_*` | ✅ (détection seulement) |
| R50 | §11 BLIND : agir **et geler** | `decision.py:293-321,627-633` | `test_blind_*`, `test_a_blind_bot_only_takes_life_safety_actions` | ⚠️ F6 |
| R51 | §11 mode prudent à +3 % / −4,5 % | signal seul (`latency.py:209-215`) | — | ❌ F13 |
| R52 | I1 | `invariants.py:139-146` | `test_i1_warns_on_delta_only_at_rest` | ✅ |
| R53 | I2 en croisière | `invariants.py:149-154` | `test_i2_warns_when_ltv_leaves_its_cruise_headroom` | ⚠️ m4, m5 |
| R54 | I2 « jamais » | `invariants.py:155-168,285-295` | `test_i2_turns_critical_and_deflates_when_p3_never_answers` + 3 | ⚠️ F9 |
| R55 | I3 | `invariants.py:171-192`, `schema.py:358-369` | `test_i3_*`, `test_cruise_margin_floor_must_sit_between_pump_and_target` | ✅ / ⚠️ m5 |
| R56 | I4 | `invariants.py:195-206` | `test_i4_*`, `test_reject_cushion_floor_above_target` | ✅ / ⚠️ F1 (équité) |
| R57 | I5 | `invariants.py:209-217` | `test_i5_*` | ✅ |
| R58 | I6 : WARN à 15 min, CRITICAL à 60 min | `invariants.py:220-232` | `test_i6_*`, `test_transfer_thresholds_must_escalate` | ✅ |
| R59 | I8 : ±0,5 pt après recomposition | `invariants.py:247-262` | `test_i8_checks_the_targets_after_a_recompose` | ✅ |
| R60 | valeurs par défaut des invariants = README | `schema.py:139-149` | `test_invariant_defaults_are_the_readme_numbers` | ✅ |
| R61 | §8.3 ltv_after ≤ tl+0,01 au re-centrage haut | — | — | ❌ F13 |
| R62 | §8.1 BUILD en 3 tranches, slippage ≤ 30 bps | — (swap.py est un squelette) | — | ❌ F13, O5 |
| R63 | §14 plafond LIVE_SMALL ; LIVE seulement avec un rapport M1 de moins de 30 j | — | — | ❌ F13 |
| R64 | §9.1 levier 10x isolé vérifié au boot | WARN si non isolé (`hyperliquid.py:106-112`) ; levier non vérifié | — | ❌ partiel F13 |
| R65 | §9.2 LT illisible → boot refusé | `decision.py:267-268`, `reconcile.py:57-64` | `test_boot_refuses_an_unreadable_threshold`, `test_the_cli_refuses_the_boot_cleanly_in_every_mode` | ✅ |
| R66 | §9.2 HF local comme contrôle | — | — | ❌ F13 |
| R67 | §4 bandes coussin plein et coussin vide dans le classeur | `classeur.py:146-156` | `test_the_classeur_runs_on_an_explored_target_and_a_rounded_config` | ✅ / ⚠️ m1, m2 |
| R68 | §1 largeur du flanc haut publiée | — | — | ❌ m8 |
| R69 | §4 exposition = spot / équité | `classeur.py:74-76` (collatéral / capital) | — | ⚠️ m3 |

**Solde :** 69 lignes. 39 soldées, 17 en écart, 12 sans code, 2 sans test (R10 et R14 ; une ligne compte double). Le rapprochement n'est pas à zéro.

### Code financier sans règle README (décisions non spécifiées)

- Montant de P5 : marge cible − marge actuelle (`decision.py:444-445`).
- Montant de P6 : dette − (tl + 0,01) × collatéral Aave (`decision.py:463-465`, F7).
- `MIN_LTV_MARGIN_TO_LT`, `_HF_ALERT_FLOOR`, `_ANCHOR_DRIFT_ALERT`, la dérive de dette max(50 $, 5 %) et les `+0.01` de P4 et P6 (m11).
- Premier écrémage immédiat (m10).
- BLIND AAVE_ONLY : une tranche par cycle, sans condition sur le HF (F6).
- Backtest :
  - zone morte de P10 = `skim_min_usd` ;
  - `preempt=False` (F4) ;
  - liquidation HL à `config.maintenance_margin` ;
  - LT 0,79 appliqué au coussin (m1).
- Classeur : `FUNDING_APR = 0.11` et provisions fixes (F11).

---

## 9. Ordre d'attaque proposé

1. **F1 (réserve HL = `total − hold`)** et **F3 (signe de la position).** Correctifs courts, impact non borné ou de 22 % sur le dimensionnement. Ajouter des fixtures avec `hold` non nul et `szi` positif.
2. **D1 et D2 sur notre compte.** À la première position LIVE_SMALL ou sur une micro-position dédiée : relever `marginUsed`, `hold` et `liquidationPx`. Ajouter un déclencheur du flanc haut sur la distance à `liquidationPx`.
3. **F5 (MM et `maxLeverage`).** Comparaison à chaque cycle, alerte en cas de divergence, refus si `margin_ratio_reduce` ≤ MM.
4. **F8 (gouvernance Aave en cours de route).** `bands_incoherence` à chaque cycle, condition « cible de remboursement < seuil de pompe » et « re-centrage avant pompe », gel si le LT lu est incohérent.
5. **F4, F6, F10 (préemption, gel, REPAIRING, cadence de P10).** À intégrer au moteur de la phase 3, avant que `decide()` ne pilote quoi que ce soit. Passer le backtest à `preempt=True` par défaut et relancer les campagnes.
6. **Décisions de spec de l'opérateur** : O1 (cible LTV), F7 et m4 (base de LTV de P6, P4 et I2), F9 (I2 contre P3 et P4), O2 (porte de régime), O4, O5. Puis report dans le README et le code.
7. **F2 (pagination du funding).** Obligatoire avant le câblage de la porte (chantier 8.6). Aligner la production sur `backtest/hl_funding.py`.
8. **F11 et m9 (classeur).** Provisions proportionnelles au capital, ligne de liquidation HL, hypothèses de carry rapprochées des lectures du jour, publication du flanc haut (m8). Retirer le chiffre dérivé du README §12.
9. **Valeurs NON VÉRIFIÉES (§7).** Lire `swap_fee`, `slippage` et le gaz sur fork ou par un devis de routeur, et basculer à `verified=True` les frais HL désormais lus.
10. **MINEURS restants** (§5), par lot de documentation.

**À refaire avant LIVE_SMALL** : une nouvelle `/revue-finance` sur le commit qui portera ces corrections, dans une session vierge.
