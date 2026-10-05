# Audit finance — 2026-10-05 — commit a26d103 (main)

Revue `/revue-finance`, à contexte frais : la session qui a produit ce rapport n'a ni écrit ni modifié le code audité. Méthode : trois sous-agents à contexte frais, en lecture seule et en parallèle : A (rapprochement spec → code → test), B (unités et conventions), C (paramètres réels et scénarios). Consolidation et contre-vérifications ponctuelles par la session principale. Aucune correction de code pendant la revue.

Seconde revue après les correctifs de la PR #6 (`ce63d79..a26d103`), qui répondaient à `audit-finance-2026-10-02-ce63d79.md`.

**Verdict : rapprochement NON SOLDÉ.** 2 écarts MAJEURS nouveaux, 1 écart dont la gravité est disputée (MAJEUR ou MINEUR, §4 D1), et 7 MAJEURS du 2 octobre encore ouverts ou partiels. Le passage LIVE_SMALL reste bloqué.

**Ce que la PR #6 a réellement soldé** (vérifié en code, en test et en réel) :
- **F1 (formule)** : la réserve libre HL = `total − hold`.
- **F2** : la moyenne de funding 30 j vaut 0,1019124238, identique jusqu'au 17e chiffre à une lecture paginée indépendante.
- **F3** : le signe de la position.
- **F7 pour P6** : 3 119 $ remboursés, LTV spot après 0,685.
- **F9** : P4 prend la relève de P3 au seuil coussin.
- **F10 dans le moteur.**

**Ce qui ne tient pas :**
- **N1** : la nouvelle porte de régime du README n'a jamais été codée, et un test épingle l'ancienne règle.
- **N2** : le backtest sous-vend d'un facteur 3 au désendettement P4.
- **N3** : I9, présenté comme relu « à chaque snapshot », n'est appelé par aucune boucle.
- **Flanc haut** : toujours structurellement exposé à un choc instantané de +8 %.

Contre-vérifications de la session principale :
- **N1** : `decision.py:189-195` comparé à README §8.9 l.268-282.
- **N3** : `grep check_invariants src/` ne renvoie que la définition ; `tracer.py:139` n'appelle que `decide`.
- **N2** : `backtest/ledger.py:446`, refait à la main : 2 874 $ vendus contre 9 124 $ nécessaires.

---

## 1. Référentiel 1 : valeurs réelles lues pendant l'audit (2026-10-05, ~02:00 heure de Paris)

| Paramètre | Valeur | Source de la lecture |
|---|---|---|
| wstETH Aave v3 Arbitrum : décimales, LTV max, LT, bonus | 18 ; 0,75 ; **0,79** ; 1,072 (pénalité 7,2 %) ; protocol fee 0,10 | `scripts/read_aave_params.py`, blocs 511759664 et 511760526 |
| wstETH : état, caps | actif, non gelé, non en pause ; supply cap 34 000 (19 170 déposés) ; borrow cap 1 300 | idem |
| USDC natif : décimales, LTV, LT, bonus | 6 ; 0,75 ; **0,78** ; 1,05 ; RF 0,10 | idem |
| E-mode | 4 catégories ; aucune éligible pour une dette USDC sur collatéral wstETH → e-mode 0 | idem |
| Oracle Aave | wstETH 3 396,13 $ ; WETH 2 726,61 $ ; USDC 0,999937 $ ; ratio **1,245551** (`stEthPerToken` absent sur Arbitrum) | idem |
| Taux variable USDC | **3,944 %/an APR** (4,023 % APY) ; supply 3,15 % | `Pool.getReserveData`, RPC publics, blocs 511760532 à 511762121 (agents A, B, C, concordants) |
| HL ETH : szDecimals, maxLeverage, table de marge | 4 ; 25 ; table 55 « tiered 25x » (15x au-delà de 100 M$) | API `info` `metaAndAssetCtxs` |
| HL maintenance margin ETH | 0,02 = 1/(2·25) ; dérivation reproduite par B sur 10 positions tierces (voir D2) | idem + `clearinghouseState` de comptes tiers |
| HL mark / oracle / mid | 2 726,6 / 2 727,0 / 2 726,65 (lecture de la session principale) | `metaAndAssetCtxs` |
| HL frais du compte opérateur | taker 4,5 bps ; maker 1,5 bps ; remise de parrainage 4 % ; taker spot 7 bps | API `userFees` (A, C) |
| HL funding ETH 30 j, paginé | 720 points du 05/09 01:00 au 05/10 00:00 UTC, 0 trou ; **10,19 %/an** (×8760) ; 30 heures négatives ; semaines 9,12 / 8,07 / 12,41 / 10,95 / 10,95 % | `fundingHistory` paginé (A, B, C, concordants) |
| Gaz Arbitrum | 0,0201 gwei ; un cycle Aave de 932 k gaz ≈ 0,05 $ | `eth_gasPrice`, bloc 511761842 |
| Staking wstETH | ≈ 2,13 à 2,20 %/an, **inféré** de trois lectures du ratio (15/09, 02/10, 05/10), pas mesuré sur 30 j | — |
| Notre compte HL | aucune position ; USDC spot 26,79 ; hold 0 | `clearinghouseState` / `spotClearinghouseState` |

Aucun paramètre de plateforme n'a bougé depuis le 2 octobre. **Seuils configurés contre le LT réel** :
- pompe 0,75 : 0,040 sous le LT ;
- coussin 0,765 : 0,025 sous le LT ;
- démarrage refusé si le LT tombe à 0,725 ou moins.

## 2. Bilan de référence et bandes (config de l'exemple, cible 0,675, LT 0,79)

Point fixe solveur ↔ classeur : écart de 0,0000 % (exécuté par A et B). Tout est linéaire en capital, donc les bandes en % valent pour 20 k$ comme pour 100 k$.

| Poste | 20 000 $ | 100 000 $ |
|---|---|---|
| Coussin (5 %) | 1 000 | 5 000 |
| Spot wstETH | 41 758 | 208 791 |
| Dette USDC (0,675 × spot) | 28 187 | 140 934 |
| Marge isolée HL (10 %) | 4 176 | 20 879 |
| Réserve libre HL (3 %) | 1 253 | 6 264 |
| LTV Aave / HF | 0,6592 / 1,198 | idem |

### Flanc bas (baisse de prix depuis la cible)

« Coussin vide, classeur » = coussin parti sans que la dette baisse (libellé du classeur et de `read_aave_params.py`). « Après P3 » = coussin consommé à rembourser.

| Seuil | LTV | Réel, coussin plein (LT pondéré 0,78977) | Classeur, coussin plein | Coussin vide, classeur | Après P3 |
|---|---|---|---|---|---|
| P6 pompe | 0,750 | −12,36 % | −12,39 % | −10,00 % | −13,19 % |
| P3 / P4 | 0,765 | −14,13 % | −14,16 % | −11,76 % | −14,90 % |
| Liquidation | 0,790 | **−16,92 %** | −16,95 % | −14,56 % | −17,59 % |

Le code, le classeur et le réel concordent à 0,03 pt près (m1, toujours ouvert).

### Flanc haut (MM 2 % ; `marginUsed` inclut le PnL latent, voir D2)

| Seuil | Marge / notionnel | Hausse de prix | `decide()` |
|---|---|---|---|
| I3 croisière | 0,07 | +2,80 % | NOOP (WARN) |
| P7 re-centrage haut | — | +4,50 % | RECENTER_UP |
| P5 pompe montante | 0,05 | +4,76 % | PUMP_UP 2 885 $ (14 423 $) |
| P2 urgence | 0,035 | +6,28 % | ADD_ISOLATED_MARGIN 1 253 $ (6 264 $) : la réserve, plus le double compte |
| Liquidation HL | 0,02 | **+7,84 %** | — |
| Liquidation après versement de la réserve par P2 | — | +10,78 % | — |

## 3. Scénarios rejoués (à la main, puis par `decide()` : concordance au dollar près, agent C)

| Scénario | Aave | HL | Décision du code | Montants (20 k / 100 k) |
|---|---|---|---|---|
| **ETH −15 %**, choc instantané | LTV 0,7724, LTV spot 0,7941, HF 1,0225 (seuil coussin 1,0327) ; reste −2,2 % avant liquidation | ratio 0,294 | P3 REPAY_FROM_CUSHION ×4, puis **P4 STEPWISE_DELEVERAGE** (avant la PR #6 : P6) ; LTV spot visée 0,685 | tranches de 250 $ (1 250 $) ; vente nécessaire **9 121 $ (45 604 $)** ; le backtest n'en vend que 2 873 $ (14 365 $) → N2 |
| P6 au déclenchement (−12,36 %) | — | excédent de marge 9 338 $ pour une cible de 3 660 $ | PUMP_DOWN 3 119 $ (15 594 $) ; LTV spot après 0,6850 | F7 soldé pour P6 |
| **ETH +8 %**, choc instantané | LTV 0,611, HF 1,29 | ratio 0,0185 < MM : **short liquidé** (équité isolée 835 $ contre 902 $ de MM) | P2 émis trop tard | perte ≈ 835 $ (4 176 $) + frais de liquidation, puis jambe longue nue de 41,8 k (209 k) jusqu'à P1. Identique au 02/10 |
| ETH +8 %, chemin graduel | — | P7 +4,5 %, P5 +4,76 %, P2 +6,3 % | position survivante, liquidation repoussée à +10,78 % | P5 2 885 $ ; P2 1 253 $ |
| **Décrochage du marché stETH −5 %** | oracle Arbitrum insensible au marché : snapshot inchangé | inchangé | NOOP | perte latente 2 088 $ (10 440 $), réalisée à la vente ; sur-couverture ≈ 5 % en valeur de marché |
| Variante : baisse du taux de change −5 % (slashing) | LTV 0,6931, LTV spot 0,7105, HF 1,1395 | delta −5,26 % | P8 RETRUE_SHORT vers 14,565 ETH (72,825) | perte non couverte 2 088 $ (10 440 $) |

À +6,28 % pile, `decide()` rend PUMP_UP parce que le ratio vaut 0,035002 ; P2 tire dès +6,3 %. C'est conforme à « ≤ ».

### Sensibilité à la gouvernance (agent C, 20 k ; % identiques à 100 k)

| Changement | Pompe, coussin plein / vide | Liquidation, coussin plein / vide | Contrôle au démarrage | En cours de route |
|---|---|---|---|---|
| LT 0,79 | −12,36 / −10,00 | −16,92 / −14,56 | OK | — |
| LT 0,77 | −9,90 / −7,53 | −14,70 / −12,34 | OK | bandes re-dérivées par `decide()` |
| **LT 0,75** | −7,29 / **−4,93** | −12,36 / −10,00 | **OK** | coussin vide, la pompe tire **avant** le re-centrage à −6 % ; non détecté |
| LT 0,73 | −4,53 / −2,17 | −9,90 / −7,53 | **OK** | inversion dès le coussin plein ; non détectée |
| LT ≤ 0,725 | — | −9,26 / −6,90 | refus | I9 rendrait CRITICAL, **mais I9 n'est pas appelé** (N3) |

| maxLeverage HL | MM | Liquidation | Écart P2 → liquidation | Après réserve | Contrôle |
|---|---|---|---|---|---|
| 25 | 0,020 | +7,84 % | 1,56 pt | +10,78 % | OK |
| 20 | 0,025 | +7,32 % | 1,04 pt | +10,24 % | OK ; WARN I9, non appelé |
| **15** | 0,0333 | +6,45 % | **0,17 pt** | +9,35 % | **OK** ; WARN seulement |
| 14 | 0,0357 | +6,21 % | −0,07 pt | +9,10 % | refus au démarrage ; CRITICAL I9, non appelé |
| 10 | 0,05 | +4,76 % | −1,52 pt | +7,62 % | idem |

Le LT et la MM sont bien relus à chaque snapshot (`watcher.py:72,146,157`), et `decide()` re-dérive les bandes Aave à chaque cycle. Ce qui manque, c'est le contrôle de cohérence (I9) et la réaction. La LTV max est lue (`watcher.py:150`) mais n'est utilisée par rien (m12) : une coupe sous 0,685 bloquerait le borrow de P5 et ne se verrait qu'à l'exécution.

---

## 4. Désaccords entre agents (règle 4 : remontés côte à côte, pas votés)

**D1. Gravité de N3 (I9 et la relecture de gouvernance ne sont câblés dans aucune boucle).**
- **C : MAJEUR.** Une coupe de gouvernance en cours de route (LT 0,79 → 0,73, ou maxLeverage 25 → 15) passe sans alerte ni gel. Pour un LT lu à 0, P3, P4 et P6 deviennent muets (`derive_bands(0)`), et I9 est la seule défense prévue. Le correctif c34ec1c est présenté comme « I9 relit la gouvernance à chaque snapshot », alors que rien ne l'exécute.
- **A : MINEUR (documentation).** Aucune boucle de production n'exécute encore d'action (F13). Le README l.304 reconnaît déjà que « rien ne revérifie le LT en cours de route ». Ce sont les lignes l.112, l.113 et l.346, ainsi que le message de commit, qui affirment à tort l'inverse.
- B a cru I9 exécuté à chaque snapshot. C'est une erreur de fait, corrigée par la session principale (grep), pas une troisième position. Le statut de **F5** et de **F8** en découle : SOLDÉ selon B, PARTIEL selon A et C ; la session principale retient PARTIEL sur les faits.
- À trancher par l'opérateur. Tant que ce n'est pas tranché, le point est traité comme bloquant pour LIVE_SMALL.

**D2. Niveau de preuve de la sémantique de `marginUsed` (PnL latent compris) et de la MM à 2 % : les anciens D1 et D2 du 02/10.**
- **B : VÉRIFIÉ.** Sur 10 positions ETH isolées de comptes tiers, de 1x à 25x dont 2 shorts, le `liquidationPx` publié est reproduit à 0,0000 % avec `marginUsed` = équité PnL compris et MM = 0,02. L'hypothèse « sans PnL » se trompe de 0,05 à 2,1 %. Sur 6 comptes, `hold == marginUsed` au centime.
- **C : probablement soldé**, mêmes mesures tierces (hold = Σ marginUsed à 1e-4 près, PnL compris). Pas de vérification sur notre compte.
- **A : NON VÉRIFIÉE**, faute de position ouverte sur notre compte (hold 0).
- À trancher par l'opérateur : la reproduction sur comptes tiers suffit-elle, ou faut-il une micro-position sur notre compte avant LIVE_SMALL ?

**D3. Gravité de m9 (hypothèses de carry du classeur contre les lectures du jour).** B : MINEUR, ce sont des hypothèses de scénario et non des paramètres de plateforme. B reconnaît qu'une lecture stricte de la règle « 1 contredit 3 = MAJEUR » le classerait MAJEUR. À trancher par l'opérateur.

---

## 5. Écarts chiffrés

### MAJEURS nouveaux

**N1. La porte de régime du code n'est pas celle du README §8.9 (arbitrage O2 écrit par cc98fa7, jamais codé).** Agents A, B et C, contre-vérifié.
- **Spec** :
  - `f_star = target_ltv·borrow_30d − staking_30d` ; `regime_spread = funding_30d − f_star` ;
  - exposition pleine si le spread est ≥ `spread_full_bps`, moitié s'il est ≥ `regime.safety_margin_bps`, sinon 0 ;
  - les trois termes sont des moyennes sur la même fenêtre de 30 j.
- **Code** :
  - `decision.py:189-195` compare `carry_spread` (`types.py:175-176`), égal à funding_30d − taux d'emprunt **instantané**, à 500 bps puis à **0** ;
  - `backtest/engine.py:376` fait de même ;
  - ni `borrow_30d` ni `staking_30d` n'existent ;
  - `regime.safety_margin_bps` est absent de `RegimeConfig` (`schema.py:64-68`, `extra="forbid"`) : une config conforme au README est refusée au chargement.
- **Test** : `test_decision.py:563-579` (`test_le_regime_lit_le_spread_par_bandes`) épingle l'ancienne règle (« 0,0 → moitié »).
- **Réel aujourd'hui** : f* ≈ 0,675 × 3,944 − 2,13 à 2,20 ≈ 0,46 à 0,53 %. Spread selon le README ≈ 9,7 % ; spread du code 6,25 %. L'écart est de 0,325·borrow + staking ≈ 3,4 à 3,5 pt. Les deux règles donnent l'exposition pleine aujourd'hui.
- **Impact** :
  - Funding entre ≈ 5,5 % et 8,9 % : le README dit pleine (2,353x), le code moitié (1,5x). Spot non déployé : 14 486 $ (72 428 $). Carry perdu ≈ 940 $/an (4 700 $) à un funding de 7 %, soit 4,7 pt du capital.
  - Funding entre f* + marge de sécurité et ≈ 3,9 % : le README dit moitié, le code PARKED. Sortie complète à tort : 27 273 $ (136 364 $) de spot, avec swap, pont et frais d'ordres.
- **Portée** : latent en production (`tracer.py:296-311` passe `desired_exposure_mult=None`). Actif dans toute campagne de backtest `regime=True`. Le 8,55 %/an et l'A/B porte ON/OFF ont été mesurés sous l'ancienne règle : **à relancer** après correction.

**N2. Le désendettement P4 du backtest sous-vend d'un facteur ≈ 3.** Agent C, contre-vérifié.
- **Spec** : P4 rembourse jusqu'à une LTV spot ≤ `target_ltv + 0,01` (README, F7 arbitré).
- **Code** : `backtest/ledger.py:446-447` calcule `excess = dette − 0,685 × spot` puis vend `excess` de wstETH. Or vendre le spot pour rembourser le fait rapetisser. La vente juste est X = (D − t·S)/(1 − t), soit un facteur 1/0,315 = 3,17.
- **Scénario −15 %** : vente de 2 874 $ au lieu de 9 124 $ (14 365 $ contre 45 604 $ à 100 k). La LTV spot finit à **0,7453** au lieu de 0,685, avec un HF de 1,06 qui fait taire P4.
- **Bande restante avant liquidation** : −5,65 % au lieu de −13,3 %, soit **−7,6 pt** jusqu'à ce que P7 re-centre.
- **Portée** :
  - Le défaut préexistait. Le commit 936a3a5, qui le présente comme aligné sur F7, ne l'a pas corrigé.
  - Le backtest est infidèle sur toutes les fenêtres où P4 tire : le décompte des liquidations Aave et le coût des sorties sont faussés.
  - En production, aucun exécuteur P4 n'existe encore : la formule juste est à écrire dans la spec avant de l'implémenter.

### Gravité disputée (D1)

**N3. La gouvernance n'est contrôlée qu'au démarrage.**
- **Câblage** : `check_invariants` (`invariants.py:315`) n'est appelé par aucun code de `src/` ni du backtest. La seule boucle, `tracer.py:136-139`, appelle `decide()` seule.
- **Pas de distance minimale au-dessus de la MM** dans `hl_margin_incoherence` (`decision.py:291-309`) : à maxLeverage 15, P2 tire 0,17 pt avant la liquidation, soit ≈ 1 s de squeeze à +9 %/min, et il n'y a qu'un WARN.
- **Ordre re-centrage / pompe non contrôlé** par `bands_incoherence` (`decision.py:269-288`) : la pompe tire avant le re-centrage dès un LT de 0,758 avec le coussin vide, ou de 0,740 avec le coussin plein.
- **Un LT lu à 0 en cours de route** rend P3, P4 et P6 muets.
- **Documentation fausse** : README l.112, l.113 et l.346 (contredits par l.304) ; message du commit c34ec1c.
- **Impact** : une coupe de LT à 0,75 ramène la liquidation, coussin vide, de −14,56 % à −10,00 %, soit −4,6 pt de bande, sans alerte. À 100 k, c'est 209 k$ de collatéral exposés à une pénalité de 7,2 %.

### MAJEURS du 2 octobre : statut à a26d103

| # | Objet | Statut | Preuve / reste |
|---|---|---|---|
| F1 | réserve HL comptant la marge deux fois | **SOLDÉ (formule)** ; résidu D2 | `hyperliquid.py:236-241` ; tests `test_the_free_reserve_excludes_the_margin_already_committed`, `test_the_free_reserve_never_goes_negative` ; P2 ajoute 1 253 $ et non 2 885 $ |
| F2 | funding 30 j non paginé | **SOLDÉ** | `hyperliquid.py:146-184` ; 3 tests ; 720 points, valeur identique à la lecture indépendante |
| F3 | signe de la position perdu | **SOLDÉ** dans le delta ; régression m23 | `hyperliquid.py:56-66`, `watcher.py:126-133`, `types.py:131-134` |
| F4 | préemption des urgences absente | **OUVERT** | `backtest/engine.py:208` `preempt=False` ; rien dans `src` |
| F5 | MM non comparée | **PARTIEL** | refus au démarrage fait (`reconcile.py:66-73`) ; I9 non câblé ; pas de distance minimale (N3) |
| F6 | gel après BLIND, REPAIRING, verrou P2 | **OUVERT** | absents de `src` |
| F7 | cible de P6 et P4 | **SOLDÉ pour P6** ; **PARTIEL pour P4** | P6 : `decision.py:503-509`, test `test_p6_repays_down_to_the_target_on_the_spot_alone` ; P4 : pas d'exécuteur et ledger faux (N2) |
| F8 | gouvernance Aave au démarrage seulement | **PARTIEL** | pompe nulle supprimée (`decision.py:506-509`, `test_p6_does_not_pump_nothing`) ; condition resserrée ; contrôle en cours de route non câblé (N3) |
| F9 | I2 « jamais » contre P3/P4 | **SOLDÉ** | `decision.py:462-467`, `test_under_the_cushion_threshold_a_local_defence_always_fires` |
| F10 | P10 sans cadence | **SOLDÉ dans le moteur** | `decision.py:588-645`, 3 tests ; non alimenté en production (m26) |
| F11 | provisions fixes, aucune pour une liquidation HL | **OUVERT** | `classeur.py:41-50` inchangé |
| F12 | `LiquidationCall` Aave non câblé pour P1 | **OUVERT** | `tracer.py:318` |
| F13 | aucun exécuteur de P3 à P10 | **OUVERT** | phases 3 et 8 du plan |
| — | choc instantané de +8 % : liquidation HL avant P2 | **OUVERT** (structurel) | chiffres identiques au 02/10 (§3) |

### MINEURS

Nouveaux :

| # | Écart | Où | Impact |
|---|---|---|---|
| m23 | Après F3, un long a une taille négative : `margin_breached` et I3 (`invariants.py:115,179`) l'ignorent. P2, P5 et BLIND HL_ONLY versent de la marge pour **maintenir** un long, ou le réduisent vers −0,7·L, au lieu de le fermer | `invariants.py`, `decision.py:337,398-417` | probabilité faible ; P8 corrige au cycle suivant ; montant non chiffrable de façon fiable |
| m24 | I2 en croisière, sur la LTV spot (correctif m4), sonne à **−2,88 %** au lieu de −5,27 %, à l'intérieur de la bande de re-centrage (−6 %) : aggravation de m5 | `invariants.py:151-160` | WARN parasites sur 3,1 pt de prix ; question de spec : `cruise_ltv_headroom` ≈ 0,04 |
| m25 | `hold` inclut aussi la marge des ordres au repos : pendant un ordre maker (P8, re-centrage), la réserve libre et l'équité sont sous-estimées | `hyperliquid.py:236-241` | ≈ 84 $ (419 $) pour un re-truage de 2 %, côté prudent |
| m26 | P10 : l'origine de la transition est la cible précédente, pas l'exposition tenue (`engine.py:392`) ; la zone morte (`decision.py:615`) surestime le spot déplacé de ≈ 7 %, seuil effectif 187 $ au lieu de 200 $ ; la production ne fournit ni origine ni horloge (`tracer.py:304-312`) | backtest, `decision` | négligeable en montant ; convergence redevenue géométrique en production |
| m27 | Le cache du funding 30 j est indexé par heure, pas par paire ; un appel à H:00:00.0x fige une fenêtre sans la dernière ligne | `hyperliquid.py:77-80,157-160` | < 0,02 pt |
| m28 | Le funding est réglé sur l'oracle chez HL (NON VÉRIFIÉ) ; le README §2 et `backtest/ledger.py:162` le règlent sur le mark | — | 0,009 % aujourd'hui |
| m29 | `bands_incoherence` compare la pompe (LTV Aave) à `tl + 0,01` (LTV spot) | `decision.py:269-288` | prudent : refus au démarrage pour un LT entre 0,709 et 0,725 sans nécessité |
| m30 | Code financier sans règle README : montant de P5 (`decision.py:484-485`), `MIN_LTV_MARGIN_TO_LT` (`schema.py:56`), `_HF_ALERT_FLOOR` et `_ANCHOR_DRIFT_ALERT` (`reconcile.py:28-30`), dérive de dette max(50, 5 %) (`reconcile.py:110`), `_MM_TOLERANCE`, une tranche par cycle en BLIND AAVE_ONLY, premier écrémage immédiat ; backtest : `preempt=False`, LT 0,79 appliqué au coussin | divers | décisions non spécifiées |
| m31 | Commentaires et docs périmés : `schema.py:8` (« pump > cushion > deleverage »), `schema.py:389-391`, `hyperliquid.py:107-109` (« re-verify at boot », « maxLeverage e.g. 50 »), `config.yaml.example:25` (« funding30d >= borrow + 5pts »), log de `reconcile.py:76-84` (bande coussin vide non nommée), nom et commentaire « 0,775 » de `test_p4_fires_when_ltv_over_deleverage_and_cushion_empty` | divers | documentation |

Du 2 octobre :
- **Soldés** : m4 (mais voir m24), m19.
- **Partiels** :
  - m16 : frais vérifiés aujourd'hui, toujours marqués `verified=False` ; remise de 4 % non prise en compte.
  - m21 : `exposure_mult_half < exposure_mult` et la garde statique de la marge de pompe restent sans test.
- **Ouverts** :
  - m1 (0,03 pt) ; m2 ; m3 ; m5 ; m6 (mid au lieu du mark : 0,2 à 1,65 bp aujourd'hui) ; m7 ; m8 ; m9 ;
  - m10 ; m11 ; m12 ; m13 ; m14 (wstETH à 3 000 $ contre 3 396 $, −11,7 %, non conservateur) ; m15 ;
  - m17 ; m18 ; m20 ; m22 : `config.yaml` local dit encore « +8 % band », « −15,7 % » et « depuis LTV 0,70 ».

**Hypothèses du classeur contre le réel (m9)** :

| Hypothèse | Classeur | Réel |
|---|---|---|
| Funding | 11 % | 10,19 % |
| Emprunt | 5 % | 3,944 % |
| Staking | 2,7 % | ≈ 2,13 à 2,20 % |

Carry brut : 4 312 $ (21,6 %) au classeur contre ≈ 4 034 à 4 062 $ (≈ 20,2 %) réel, soit ≈ −265 $ à 20 k et ≈ −1 300 $ à 100 k.

---

## 6. Questions de spec pour l'opérateur (aucune tranchée par la revue)

- **O1. Cible LTV** (toujours ouvert) : README et config à 0,675 ; la mémoire du backtest a retenu 0,625. Toutes les bandes ci-dessus sont à 0,675.
- **O2bis. Valeur de `regime.safety_margin_bps`** : spécifiée nulle part. Elle est nécessaire pour coder N1.
- **O3, O4, O5** du 02/10 : ouverts (faisabilité de P4 sans flashloan, P1 après une liquidation HL, garde de slippage pendant un décrochage).
- **O7. Distance minimale entre P2 et la MM lue (N3)** : le README n'exige que « réduction > MM ». Faut-il exiger, par exemple, ≥ 1 pt ?
- **O8. Ordre re-centrage / pompe** : faut-il refuser (ou alerter) toute configuration où la pompe tire avant le re-centrage bas, coussin vide compris ?
- **O9. Formule de vente de P4 (N2)** : l'écrire dans le README, avec X = (D − t·S)/(1 − t) et la décote du marché, avant d'écrire l'exécuteur.
- **D1, D2, D3** ci-dessus.

## 7. Valeurs NON VÉRIFIÉES restantes

| Valeur | Raison |
|---|---|
| `total`, `hold` et `marginUsed` sur **notre** compte avec une position ouverte | aucune position ; seuls des comptes tiers ont été mesurés (D2) |
| Staking wstETH 30 j (≈ 2,13 à 2,20 %) | inféré de trois lectures du ratio |
| Taux d'emprunt USDC moyen sur 30 j | seul l'instantané a été lu (3,944 %) |
| Minimum d'ordre HL de 10 $ | aucun endpoint |
| Prix de règlement du funding HL (oracle ou mark) | non documenté par l'API |
| Retrait de marge isolée HL : le PnL latent compte-t-il (P6, P9) ? | jamais observé |
| Comportement de HL si `maxLeverage` baisse sur une position ouverte | jamais observé |
| Close factor Aave, règle « LTV 0 » | non lus |
| Frais et slippage du swap, gaz du swap et du pont, frais du pont, frais de retrait 1 USDC | aucune lecture possible par l'API HL ou le RPC |
| Budget de 60 s de P4 (O3) | jamais mesuré |

---

## 8. Table de rapprochement : règle → code → test (agent A)

Légende : ✅ soldé · ⚠️ écart · ❌ sans code · 🔶 sans test. Lignes de README à a26d103.

| # | Règle (README) | Code | Test | Statut |
|---|---|---|---|---|
| R1 | §3 l.100 : spot = m(E−c)/(1+m·r) | decision.py:126-134 | test_target_state.py::test_reference_balance_matches_the_classeur, ::test_the_reference_balance_sheet_is_a_fixed_point ; test_properties.py::test_the_solver_is_a_fixed_point_for_any_equity | ✅ |
| R2-R5 | notionnel, marge, réserve, dette = 0,675 × spot | decision.py:135-138 | test_target_state.py::test_solver_invariants, ::test_the_reserve_is_not_leveraged, ::test_the_resulting_ltv_sits_below_the_nominal_target | ✅ |
| R6 | le coussin sort de l'équité déployable | decision.py:126-128 | ::test_the_cushion_is_not_leveraged, ::test_solver_rejects_a_cushion_that_swallows_the_equity | ✅ |
| R7 | point fixe classeur | classeur.py:220-230 | ::test_the_solver_and_the_classeur_agree | ✅ (0,0000 %) |
| R8 | target_margin_ratio = 1/lev | schema.py:347-355 | test_config_loader.py::test_target_margin_matches_leverage | ✅ |
| R9 | exposure_mult = 1/(1−tl+1/lev) | schema.py:334-342 | ::test_exposure_mult_matches_formula, ::test_reject_bad_exposure_mult | ✅ |
| R10 | half < full | schema.py:343-344 | — | 🔶 |
| R11 | marge pompe > marge coussin ; P3 et P4 partagent le seuil | schema.py:106-110 | ::test_reject_ltv_margin_order ; test_bands.py::test_priorities_keep_their_order | ✅ |
| R12 | seuil = LT − marge ; HF = LT/(LT−m) | decision.py:236-262 | test_bands.py::test_bands_derive_from_the_on_chain_threshold, ::test_every_band_leaves_room_before_liquidation | ✅ |
| R13 | démarrage refusé si pompe ≤ tl + 0,01 | decision.py:269-288, reconcile.py:56-63 | test_bands.py::test_the_pump_must_sit_above_what_p6_repays_down_to, ::test_boot_refuses_when_the_pump_would_fire_at_rest | ✅ ; m29 |
| R14 | garde statique sur la marge de pompe | schema.py:396-400 | — | 🔶 |
| R15 | marge la plus serrée ≥ 0,01 | schema.py:56,111-116 | ::test_reject_margin_too_close_to_liquidation | ✅ ; constante sans ligne README |
| R16 | pompe HL > réduction HL | schema.py:97-101 | ::test_reject_reduce_above_pump | ✅ |
| R17 | réduction > MM lue : refus au démarrage, I9 à chaque snapshot, WARN si MM ≠ config | decision.py:291-309, reconcile.py:66-73, invariants.py:271-296 | test_reconcile.py::test_boot_refuses_p2_under_the_hyperliquid_maintenance_margin ; test_invariants.py::test_i9_* | ⚠️ démarrage ✅ ; I9 non appelé (N3) ; pas de distance minimale |
| R18 | LT et LTV max lus à chaque cycle | watcher.py:148-150, aave.py:292-320 | — | ✅ LT ; ⚠️ LTV max inutilisée (m12) |
| R19-R21 | prix oracle, ratio wstETH/ETH, collatéral, LTV Aave | types.py:95-114, aave.py:225 | test_snapshot.py::test_spot_notional_delta_zero_at_target, ::test_delta_follows_the_staking_rate, ::test_ltv_matches_target | ✅ |
| R22 | LTV spot = dette/spot | types.py:116-128 | test_invariants.py::test_i2_in_cruise_reads_the_spot_ltv_not_aave_s ; test_decision.py::test_p6_repays_down_to_the_target_on_the_spot_alone | ✅ |
| R23 | notionnel = abs(taille) × mark | types.py:131-134 | test_hl_reader.py::test_a_long_keeps_its_sign_instead_of_passing_for_a_short | ✅ ; m6 (mid) |
| R24 | margin_ratio = marge / notionnel | types.py:137-140 | test_snapshot.py::test_margin_ratio | ✅ ; sémantique D2 |
| R25 | delta en ETH, signé | types.py:143-162, hyperliquid.py:56-66, watcher.py:126 | test_decision.py::test_p8_sees_a_long_instead_of_a_flat_delta ; test_hl_reader.py::test_a_short_reads_as_a_positive_short_size | ✅ ; m23 |
| R26 | funding_30d = moyenne sur 720 h × 8760 | hyperliquid.py:146-184 | test_hl_reader.py::test_the_30_day_funding_reads_the_whole_window_not_its_oldest_500_hours, ::test_the_30_day_funding_is_read_once_per_hour, ::test_a_funding_page_that_goes_back_in_time_is_a_venue_error | ✅ (vérifié en réel) |
| R27 | borrow_apr : ray → APR | aave.py:334-340 | test_aave_multicall | ✅ |
| R28 | équité ; réserve HL = USDC non engagé | types.py:179-206, hyperliquid.py:208-242 | test_hl_reader.py::test_the_free_reserve_*, test_snapshot.py::test_equity_reconstruction | ✅ formule ; D2 ; m25 |
| R29 | P1 : short = spot restant | decision.py:357-369 | test_decision.py::test_p1_liquidation_event_fires_first, ::test_blind_hl_only_still_honours_liquidation_event | ⚠️ F12, O4 |
| R30-R31 | P2 : marge ≤ 0,035 ; montant vers la cible, plafonné à la réserve | decision.py:392-414 | ::test_p2_edge_at_threshold_fires, ::test_p2_edge_just_above_threshold_does_not_fire ; test_properties.py::test_p2_never_spends_more_than_the_reserve_nor_spends_it_in_vain | ✅ |
| R32-R33 | repli de P2 puis REPAIRING (§8.6.2-3) | decision.py:416-432 ; REPAIRING absent | — | ❌ F6 |
| R34-R35 | P3 : seuil coussin, coussin ≥ tranche (25 %) | decision.py:435-450, 312-320 | ::test_p3_edge_at_threshold, ::test_p3_edge_below_threshold | ✅ |
| R36 | P4 : même seuil, coussin < tranche, cible LTV spot ≤ tl + 0,01 | decision.py:453-477 | test_bands.py::test_under_the_cushion_threshold_a_local_defence_always_fires ; test_decision.py::test_p4_fires_when_ltv_over_deleverage_and_cushion_empty | ✅ déclencheur ; ⚠️ montant faux au backtest (N2), pas d'exécuteur |
| R37 | P5 : marge ≤ 0,05 | decision.py:480-495 | ::test_p5_fires_below_pump_threshold, ::test_p2_takes_priority_over_p5 | ✅ déclencheur ; montant non spécifié (m30) |
| R38 | P6 : HF ≤ seuil pompe ; rembourse jusqu'à LTV spot ≤ tl + 0,01 ; pas de pompe à 0 | decision.py:498-518 | ::test_p6_fires_at_ltv_pump, ::test_p6_repays_down_to_the_target_on_the_spot_alone ; test_bands.py::test_p6_does_not_pump_nothing | ✅ (3 119 $ / 15 594 $) |
| R39 | P7 : +4,5 % / −6 % | decision.py:521-545 | ::test_p7_* (5 tests) | ✅ |
| R40 | P8 | decision.py:548-563 | ::test_p8_fires_when_the_hedged_quantity_drifts, ::test_p8_ignores_a_pure_price_move | ✅ |
| R41 | P9 : excédent > skim_min, créneau | decision.py:566-585, 661-696 | ::test_p9_* (3 tests) | ⚠️ m10 |
| R42 | §8.5 SKIM 3a, 3b, 4, 5 | — | — | ❌ F13 |
| R43 | **§8.9 : regime_spread = funding_30d − (tl·borrow_30d − staking_30d) ; bande basse = safety_margin_bps** | decision.py:189-195 (ancienne règle) | test_decision.py:563-579 épingle l'ancienne règle | ❌ **N1** |
| R44 | hystérésis | decision.py:198-210 | ::test_la_porte_ne_bouge_pas_avant_la_confirmation + 2 | ✅ |
| R45 | évaluation quotidienne à 00:00 UTC | backtest/engine.py:362-374 seulement | — | ❌ F13 |
| R46 | P10 : 25 % de l'écart initial, une tranche par heure, zone morte skim_min_usd | decision.py:588-645 | ::test_p10_reaches_the_target_in_four_tranches_not_forty_eight, ::test_p10_takes_one_tranche_per_hour_at_most, ::test_p10_does_not_trade_a_drift_worth_less_than_an_operation | ✅ moteur ; m26 |
| R47 | exposition mesurée en inversant le solveur | decision.py:149-166 | ::test_exposure_mult_of_inverse_exactement_le_solveur | ✅ ; m20 |
| R48 | §6 préemption des urgences | — ; backtest `preempt=False` | — | ❌ F4 |
| R49 | I7 | invariants.py:241-250 | ::test_i7_two_executions_at_once_is_critical_without_deflating | ✅ (détection) |
| R50 | BLIND : agir **et geler** | decision.py:326-354, 709-714 | ::test_blind_* ; test_properties.py::test_a_blind_bot_only_takes_life_safety_actions | ⚠️ F6 (pas de gel) |
| R51 | mode prudent | signal seul (latency.py) | — | ❌ F13 |
| R52 | I1 | invariants.py:141-148 | ::test_i1_warns_on_delta_only_at_rest | ✅ |
| R53 | I2 en croisière, sur la LTV spot | invariants.py:151-160 | ::test_i2_in_cruise_reads_the_spot_ltv_not_aave_s | ✅ ; m24 |
| R54 | I2 « jamais » (P3 ou P4) | invariants.py:161-174 | ::test_i2_turns_critical_and_deflates_when_p3_never_answers + 3 | ✅ |
| R55 | I3 | invariants.py:177-198, schema.py:357-368 | ::test_i3_*, ::test_cruise_margin_floor_must_sit_between_pump_and_target | ✅ ; m23, m5 |
| R56 | I4 = plancher × équité | invariants.py:201-212 | ::test_i4_warns_when_the_cushion_sinks_under_its_floor | ✅ |
| R57-R58 | I5, I6 | invariants.py:215-238 | ::test_i5_*, ::test_i6_* | ✅ |
| R59 | I8 sur la LTV spot | invariants.py:253-268 | ::test_i8_checks_the_targets_after_a_recompose | ✅ |
| R60 | **I9 : LT, MM et MM = config à chaque snapshot ; CRITICAL → gel** | invariants.py:271-300, 326-333 | ::test_i9_an_aave_governance_cut_mid_run_is_critical + 2 | ⚠️ **jamais appelé** (N3) |
| R61 | valeurs par défaut des invariants = README | schema.py:138-148 | ::test_invariant_defaults_are_the_readme_numbers | ✅ |
| R62-R66 | §8.3 ltv_after ; §8.1 BUILD et slippage ; §14 plafond LIVE_SMALL ; §9.1 levier 10x isolé au démarrage ; §9.2 HF local | — (hyperliquid.py:128-134 avertit seulement sur cross) | — | ❌ F13 |
| R67 | §9.2 LT illisible → démarrage refusé | decision.py:278-279, reconcile.py:56-63 | test_bands.py::test_boot_refuses_an_unreadable_threshold | ✅ démarrage ; en cours de route, muet (N3) |
| R68 | bandes coussin plein et coussin vide | classeur.py:146-155 | test_target_state.py::test_the_classeur_runs_on_an_explored_target_and_a_rounded_config | ⚠️ m1, m2 |
| R69 | flanc haut publié | — | — | ❌ m8 |
| R70 | exposition = spot / équité (2,09x) | classeur.py:74-76 (2,14x) | — | ⚠️ m3 |

**Paramètres de config → lecteur** :
- `capital_usd`, `cushion_pct` → decision.py:319.
- `short_leverage`, `target_ltv`, `target_margin_ratio`, `exposure_mult` → schema.py et decision.py.
- `exposure_mult_half` → decision.py:194 (backtest seulement).
- `maintenance_margin` → invariants.py:290 et le backtest.
- `cushion_floor_pct` → invariants.py:205.
- `recenter_*` → decision.py:525,535.
- `delta_tolerance` → decision.py:549, invariants.py:142.
- `skim_cron`, `skim_min_usd` → decision.py:570-616.
- `regime.*` → decision.py:191,209.
- `gas_min_eth` → invariants.py:217.
- `emergency.*` → decision.py.
- `watchdog.*` → watchdog.py, main.py:890.

Lus par aucun code (assumé dans la config) : `skim_policy`, `slippage_max_bps`, `order_style`, `live_small_cap_pct`.

Exigé par le README et absent du schéma : **`regime.safety_margin_bps`** (N1).

---

## 9. Ordre d'attaque proposé

1. **Arbitrages de l'opérateur**, qui conditionnent le reste : D1 (gravité de N3), D2 (preuve sur notre compte ou non), O1 (cible 0,675 ou 0,625), O2bis (valeur de `safety_margin_bps`), O7 et O8.
2. **Câbler `check_invariants` dans la boucle du traceur**, avec alerte, puis gel sur CRITICAL. Corriger les lignes du README l.112, l.113 et l.346. C'est petit et cela solde l'essentiel de N3, F5 et F8.
3. **Renforcer `hl_margin_incoherence` et `bands_incoherence`** selon O7 et O8. Traiter le cas d'un LT lu à 0 en cours de route.
4. **Coder N1** :
   - `safety_margin_bps` au schéma ;
   - moyennes `borrow_30d` et `staking_30d` sur la fenêtre du funding, en production et en backtest ;
   - réécrire `test_le_regime_lit_le_spread_par_bandes`.
5. **Corriger N2** : vente X = (D − t·S)/(1 − t) dans `backtest/ledger.py`, et l'écrire dans le README (O9). Puis **relancer les campagnes de backtest**, porte ON/OFF comprise, et republier le rendement par régime.
6. **Ouvrir une micro-position sur notre compte HL** pour clore D2 et le résidu de F1 (`total`, `hold`, `marginUsed`, `liquidationPx`).
7. **MAJEURS structurels restants**, à planifier dans les phases 3 et 8 : F4 (préemption), F6 (gel, REPAIRING, verrou P2), F12 (`LiquidationCall`), F13 (exécuteurs), défense du choc instantané de +8 %, F11 (provision pour une liquidation HL).
8. **Lot de MINEURS** :
   - propagation du signe d'un long : m23 ;
   - planchers de croisière : m24 et m5 ;
   - mark au lieu du mid : m6 ;
   - garde-fou du traceur : m14 ;
   - LTV max utilisée : m12 ;
   - drapeaux de `costs.py` : m16 ;
   - hypothèses du classeur : m9 ;
   - commentaires et docs : m17, m18, m22, m31 ;
   - tests manquants : m21.
