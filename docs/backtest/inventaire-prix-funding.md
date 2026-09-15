# Inventaire des sources : prix ETH et funding (backtest Delta-0)

Relevé fait le 2026-09-15 entre 22:10 et 22:16 UTC (le 16/09 vers 00:10 à Paris). Seules des API publiques ont servi, sans clé.
Scripts : `probe1.py` à `probe4.py`. Sorties brutes : `out_*.txt`, `binance_funding_full.json`, `hl_funding_full.json` (dans ce même dossier).
Toutes les heures sont en UTC.

## 1. Prix ETH Binance

| Série | Endpoint | Plus ancien point vérifié | Granularité | Limites / trous | Coût | Statut |
|---|---|---|---|---|---|---|
| Spot ETHUSDT 1h | `GET https://api.binance.com/api/v3/klines?symbol=ETHUSDT&interval=1h&startTime=0` | 2017-08-17 04:00 (O 301.13) | 1h | `limit` max 1000 (1500 est ramené à 1000 en silence). Poids mesuré : +2 par appel à limit=1000. Plafond `REQUEST_WEIGHT` : 6000/min | gratuit, sans clé | VÉRIFIÉ |
| Spot ETHUSDT 1m | idem, `interval=1m` | 2017-08-17 04:00 | 1m | idem | gratuit | VÉRIFIÉ |
| Futures USDⓈ-M ETHUSDT 1h | `GET https://fapi.binance.com/fapi/v1/klines?symbol=ETHUSDT&interval=1h&startTime=0` | 2019-11-27 07:00 (O 146, volume quasi nul au début) | 1h | `limit` max 1500 (2000 donne l'erreur -1130). Poids mesuré : +10 à limit=1500, +2 à limit=500. Plafond : 2400/min | gratuit | VÉRIFIÉ |
| Futures ETHUSDT 1m | idem, `interval=1m` | 2019-11-27 07:45 | 1m | idem | gratuit | VÉRIFIÉ |
| Futures mark price 1m | `GET /fapi/v1/markPriceKlines?symbol=ETHUSDT&interval=1m` | 2019-12-23 11:58 | 1m | idem klines | gratuit | VÉRIFIÉ |
| Futures index price 1m | `GET /fapi/v1/indexPriceKlines?pair=ETHUSDT&interval=1m` | 2019-12-23 11:58 | 1m | idem | gratuit | VÉRIFIÉ |
| Futures premium index 1m | `GET /fapi/v1/premiumIndexKlines?symbol=ETHUSDT&interval=1m` | 2019-12-24 03:26 | 1m | idem | gratuit | VÉRIFIÉ |
| Archives data.binance.vision | `https://data.binance.vision/data/{spot,futures/um}/{monthly,daily}/klines/ETHUSDT/{1m,1h}/ETHUSDT-{iv}-{AAAA-MM[-JJ]}.zip` | spot 1m : 2017-08 présent. Futures 1m : 2019-11 et 2019-12 en 404, **2020-01 présent** | fichiers mensuels et journaliers | voir le détail ci-dessous | gratuit, sans quota annoncé | VÉRIFIÉ |

Archives testées par requête HEAD (code HTTP 200 et taille en octets) :

| Fichier | spot 1m | futures 1m | spot 1h | futures 1h | futures markPrice 1m | futures fundingRate |
|---|---|---|---|---|---|---|
| 2021-05 | 2 307 771 | 2 036 282 | 44 688 | 40 016 | 1 209 462 | 913 |
| 2022-06 | 2 056 565 | 1 901 973 | 40 835 | 38 115 | 1 071 415 | 980 |
| 2025-10 | 2 145 592 | 1 996 726 | 42 832 | 39 876 | 1 015 683 | 953 |
| 2026-02 | 1 918 913 | 1 790 010 | 38 274 | 36 046 | 909 147 | 939 |

Autres fichiers vérifiés :
- journalier 1m du 2025-10-10, spot et futures : 200 ;
- mensuel spot 1m 2026-08 : 200, publié le 2026-09-07 ;
- journalier spot 1m 2026-09-14 : 200 ;
- `fundingRate` mensuel 2020-01 et 2026-08 : 200 ;
- `fundingRate` journalier 2026-09-14 : 404 (il n'existe pas d'archive journalière de funding).

### Fenêtres de stress en 1m (API, 60 bougies 1m par heure, les 60 étaient présentes à chaque fois)

La pire heure est celle dont l'écart (haut − bas) / ouverture est le plus grand dans la fenêtre. « Pic → creux » est la plus forte baisse enchaînée à l'intérieur de cette heure, lue sur les bougies 1m.

| Épisode | Marché | Pire heure | O / H / L / C | Écart de l'heure | Pic → creux (1m) | Pire bougie 1m (bas vs ouverture) |
|---|---|---|---|---|---|---|
| Mai 2021 | spot | 2021-05-19 13:00 | 2365.18 / 2599.00 / 1888.00 / 2493.69 | 30.06 % | −21.33 % (13:00 → 13:10) | 13:21, −9.64 % |
| Mai 2021 | futures | 2021-05-19 13:00 | 2332.92 / 2618.00 / **1400.73** / 2485.17 | 52.18 % | −40.00 % (13:00 → 13:10) | 13:09, −19.87 % |
| Juin 2022 | spot | 2022-06-15 18:00 | 1114.24 / 1227.29 / 1045.00 / 1171.97 | 16.36 % | −7.93 % | 18:00, −4.18 % |
| Juin 2022 | futures | 2022-06-15 18:00 | 1113.58 / 1237.18 / 1042.59 / 1171.50 | 17.47 % | −8.54 % | 18:00, −4.19 % |
| 10 oct. 2025 | spot | 2025-10-10 21:00 | 3871.31 / 4380.00 / 3435.00 / 3949.99 | 24.41 % (haut à 4380, mèche isolée) | −12.26 % (21:05 → 21:20) | 21:16, −3.77 % |
| 10 oct. 2025 | futures | 2025-10-10 21:00 | 3864.76 / 3980.68 / 3400.00 / 3941.17 | 15.02 % | −13.08 % (21:05 → 21:20) | 21:16, −5.26 % |
| Févr. 2026 | spot | 2026-02-06 00:00 | 1826.83 / 1892.65 / 1747.80 / 1870.61 | 7.93 % | −4.68 % | 00:19, −1.78 % |
| Févr. 2026 | futures | 2026-02-06 00:00 | 1824.94 / 1892.61 / 1736.02 / 1869.28 | 8.58 % | −5.24 % | 00:19, −2.34 % |

Précision sur juin 2022 : la plus forte amplitude sur une heure tombe le 15/06, mais le plus bas quotidien du mois est le 18/06 (futures 1d L = 878.04).

Le 2021-05-19, la mèche futures à 1400.73 (spot : 1888) est un artefact du carnet futures, qui n'a pas touché le spot. Le choix entre spot et futures change donc radicalement le résultat du stress-test.

Autres points de comparaison le 2025-10-10 dans l'heure de 21:00 :
- mark Binance futures : bas 1m 3425.34 (à 21:20) ;
- spot ETHUSDC : bas 3378.00.

## 2. Funding Binance ETHUSDT perp

| Série | Endpoint | Profondeur vérifiée | Périodicité réelle | Limites / pièges | Coût | Statut |
|---|---|---|---|---|---|---|
| Taux de funding | `GET https://fapi.binance.com/fapi/v1/fundingRate?symbol=ETHUSDT&startTime=..&endTime=..&limit=1000` | 2019-11-27 08:00 → 2026-09-15 16:00, soit 7454 lignes | **8 h sans exception** : 7453 écarts sur 7453 valent 8.0 h. `fundingInfo` affiche aussi `fundingIntervalHours: 8` | **Piège** : `startTime=0` ne renvoie que les 500 lignes les plus récentes (à partir du 2026-04-02). Il faut paginer par fenêtres `startTime`+`endTime` explicites (300 jours par appel ici). `limit=1500` est rejeté. Pas d'en-tête de poids dans la réponse ; le poids réel reste NON VÉRIFIÉE | gratuit | VÉRIFIÉ |
| Mark price à l'instant du funding | champ `markPrice` de la même réponse | vide jusqu'en 2023 (910 lignes vides en 2023), rempli ensuite | 8 h | inutilisable avant mi-2023 | gratuit | VÉRIFIÉ |
| Bornes du taux | `GET /fapi/v1/fundingInfo` | valeur du jour : cap +0.300 % / floor −0.300 % | — | historique des bornes non servi. Min/max observés sur tout l'historique : −0.356 % / +0.375 % par période, donc les bornes ont changé au fil du temps | gratuit | VÉRIFIÉ (valeur du jour seulement) |
| Archive mensuelle | `data.binance.vision/.../fundingRate/ETHUSDT/ETHUSDT-fundingRate-AAAA-MM.zip` | 2020-01 → 2026-08 (2019-11 et 2019-12 en 404) | 8 h | pas de fichier journalier | gratuit | VÉRIFIÉ |

## 3. Hyperliquid (`POST https://api.hyperliquid.xyz/info`)

| Série | Requête | Profondeur vérifiée | Granularité | Limites / trous | Coût | Statut |
|---|---|---|---|---|---|---|
| Funding ETH | `{"type":"fundingHistory","coin":"ETH","startTime":ms[,"endTime":ms]}` | **2023-05-12 00:00** → 2026-09-15 22:00, soit 28 781 lignes en 58 pages | **8 h du 2023-05-12 au 2023-06-08**, puis **horaire**. Répartition des écarts : 28 692 × 1 h, 79 × 8 h, 3 × 2 h (2023-07-02 19h, 2023-08-23 19h, 2024-08-15 12h) | **500 lignes max par réponse**, pagination par `startTime = dernier time + 1`. Les horodatages portent quelques ms de décalage (…048). Chaque ligne donne aussi `premium`. Pendant la phase 8 h, on ne sait pas si le taux est exprimé par 8 h ou par heure : NON VÉRIFIÉE | gratuit, sans clé ; poids de la requête NON VÉRIFIÉE | VÉRIFIÉ |
| Bougies ETH 1m | `{"type":"candleSnapshot","req":{"coin":"ETH","interval":"1m","startTime":..,"endTime":..}}` | **seulement les ~5000 dernières bougies** : 5088, à partir du 2026-09-12 09:25. Une demande sur 2025-10-10 renvoie une liste vide | 1m | fenêtre glissante, pas d'historique | gratuit | VÉRIFIÉ |
| Bougies ETH 1h | idem, `1h` | à partir du **2026-02-19 13:00** (5002 bougies). Févr. 2026 avant le 19 et oct. 2025 : vide | 1h | idem | gratuit | VÉRIFIÉ |
| Bougies ETH 2h / 4h / 8h / 12h | idem | 2h : depuis le 2025-07-26 · **4h : depuis le 2024-06-04** · 8h : depuis le 2022-06-16 · 12h : depuis le 2022-01-01 | — | plafond de ~5000 bougies par intervalle | gratuit | VÉRIFIÉ |
| Bougies ETH 1d | idem, `1d` | depuis le 2020-08-19 (2219 bougies) | 1d | **avant le 2023-02-26, v=0 et n=0** : ce ne sont pas des échanges HL. Les O/H/L/C du 2021-05-19 et du 2022-06-18 sont **identiques au centime aux futures Binance**, ce qui indique un remplissage avec les données Binance futures. Première bougie avec du volume : 2023-02-26 | gratuit | VÉRIFIÉ |
| Mark / oracle historiques | `metaAndAssetCtxs` | **valeur courante uniquement** (`markPx`, `oraclePx`, `premium`, `funding`) | — | aucun endpoint public vérifié ne sert l'historique du mark ou de l'oracle | — | VÉRIFIÉ (absence dans l'API info) |
| Archive S3 `hyperliquid-archive` (us-east-1) | `https://hyperliquid-archive.s3.amazonaws.com/?list-type=2` et l'objet `asset_ctxs/20251010.csv.lz4` | 403 « Anonymous users cannot invoke requests against Requester Pays buckets » | — | **requester-pays** : il faut un compte AWS authentifié et payer le transfert. Contenu, profondeur et présence du mark/oracle : NON VÉRIFIÉE (aws CLI absent, pas de compte) | payant (AWS) | accès VÉRIFIÉ, contenu NON VÉRIFIÉE |
| Archive S3 `hl-mainnet-node-data` (ap-northeast-1) | `https://hl-mainnet-node-data.s3.amazonaws.com/?list-type=2` | 403, requester-pays | — | idem | payant | accès VÉRIFIÉ, contenu NON VÉRIFIÉE |
| `stats-data.hyperliquid.xyz/Mainnet/funding_rate` | GET | 403 Access Denied | — | — | — | VÉRIFIÉ (fermé) |

## 4. Écart entre sources pendant les krachs

Il n'existe pas de bougie HL 1m ou 1h pour le 10/10/2025. La comparaison se fait donc sur la plus fine granularité servie, le 2h/4h.

| Tranche | Bas HL (bougies, prix échangé) | Bas Binance futures | Écart |
|---|---|---|---|
| 2025-10-10 20:00–22:00 (HL 2h) / 20:00–24:00 (BN 4h) | **3241.2** | 3400.00 (last, à 21:20 en 1m) · mark 3425.34 · spot USDT 3435.00 · spot USDC 3378.00 | HL **−4.67 %** sous le last Binance futures, −5.38 % sous le mark Binance |
| 2025-10-10 12:00–16:00 (4h) | 4072.1 | 4067.44 | +0.11 % |
| 2025-10-10 16:00–20:00 (4h) | 3948.0 | 3947.77 | ≈ 0 |
| 2026-02-06 00:00–04:00 (4h) | 1740.7 | 1736.02 | +0.27 % |
| 2026-02-05 20:00–24:00 (4h) | 1817.9 | 1813.44 | +0.25 % |

Hors mèche extrême, les prix HL et Binance futures restent à ±0.3 %. Pendant la cascade du 10/10, le prix échangé sur HL est descendu nettement plus bas.

HL liquide sur son **mark**, pas sur le dernier prix. Le bas du mark HL ce jour-là est NON VÉRIFIÉE : c'est l'information qui manque pour trancher.

Le funding HL du 10/10 est lu (horaire). Il atteint −0.0386 %/h à 22:00, avec un premium de −0.358 %, et −0.0230 %/h à 23:00.

## Conséquences pour le backtest

**Points bloquants**
1. **Pas de mark ni d'oracle HL historique en accès public.** La liquidation du short (isolé, levier 10, sur mark HL) ne peut pas être rejouée fidèlement. Seule piste : le S3 requester-pays (compte AWS, frais), dont le contenu reste à vérifier. Sinon il faut un proxy explicite, avec une marge de sécurité mesurée.
2. **Pas de bougies HL fines sur les épisodes de stress.** Le 1m ne couvre que ~3,5 jours et le 1h ne remonte qu'au 2026-02-19. Pour le 10/10/2025, il n'y a que du 2h/4h. Pour mai 2021 et juin 2022, HL ne cotait pas ETH (bougies 1d copiées de Binance futures, v=0).
3. **L'écart de mèche est réel.** Le 10/10/2025, le bas HL est 4.7 % sous Binance futures. À levier 10 (liquidation vers −9 à −10 % selon la maintenance margin, NON VÉRIFIÉE ici), un proxy Binance brut sous-estime le risque de liquidation d'environ la moitié de la marge. Inversement, la mèche futures Binance du 19/05/2021 (−40 % en 10 min) sur-estime ce qu'un mark lissé aurait fait.

**Proxys nécessaires**
- Prix de la jambe perp avant l'existence de HL et en 1m partout : Binance futures 1m (API ou archives dès 2020-01), en suivant plutôt le **mark Binance 1m** (dès 2019-12-23) comme proxy du mark HL. Ajouter un **choc de mèche HL paramétrable**, calibré au minimum sur le 10/10/2025 (−4.7 % vs last, −5.4 % vs mark).
- Prix du collatéral wstETH / emprunt : hors de ce lot. La référence ETH peut venir du spot Binance 1m, présent depuis 2017.
- Funding HL avant le 2023-05-12 : proxy Binance 8h ramené à l'heure (÷8). Il faut documenter que le funding HL est plafonné et calculé autrement (bornes NON VÉRIFIÉES ici). La plage de chevauchement 2023-06 → 2026-09 (horaire HL et 8h Binance) permet de calibrer le rapport entre les deux.

**Raccords entre sources**
- Funding HL : traiter la bascule 8h → 1h du 2023-06-08, et combler les 3 trous de 2 h (interpolation ou taux nul, à décider).
- Funding Binance : paginer avec `startTime`/`endTime` explicites (piège de `startTime=0`). La périodicité est 8 h constante depuis 2019-11-27, sans changement observé. Le `markPrice` inclus n'existe qu'à partir de 2023.
- Bougies : aligner les horodatages (HL `t` à la ms, funding HL décalé de quelques ms, funding Binance à +1 ms) sur une grille UTC. Les archives data.binance.vision paraissent en moyenne quelques jours après la fin du mois (fichier 2026-08 publié le 2026-09-07). Le mois courant se complète par l'API.
- Débuts de séries : spot 2017-08-17, futures 2019-11-27 (volume quasi nul les premières semaines, à écarter), mark/index Binance 2019-12-23, funding Binance 2019-11-27, funding HL 2023-05-12, premier volume HL 2023-02-26.
- Volumétrie : un an de 1m tient en ~525 000 bougies, soit ≈ 351 appels futures à limit=1500 (poids 10, donc ~3510 de poids, à étaler sur plus de 2 min vu le plafond de 2400/min). Les archives mensuelles (~2 Mo/mois) sont nettement plus économes.
