# Inventaire des sources : taux et staking (backtest Delta-0)

Date des appels : 2026-09-16 (horloge des serveurs : 2026-09-15 22:10 à 22:30 UTC). Chaque valeur ci-dessous a été lue pendant cette session. Les scripts et les sorties brutes sont rangés dans ce dossier (`llama.py`, `arb_old*.py/.txt`, `eth_probe.txt`, `eth_hist.txt`, `chainlink.txt`, `aaveapi_*.json`, `chart_*.json`).
Ce qui n'a pas pu être vérifié par un appel est marqué NON VÉRIFIÉE.

Adresses utilisées :
- Aave v3 Pool sur Arbitrum : `0x794a61358D6845594F94dc1DB02A252b5b4814aD`
- Réserves Arbitrum :
  - USDC natif `0xaf88d065e77c8cC2239327C5EDb3A432268e5831`
  - USDC.e `0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8`
  - wstETH `0x5979D7b546E38E414F7E9822514be443A4800529`
- Aave v2 LendingPool sur mainnet : `0x7d2768dE32b0b80b7a3454c06BdAc94A69DDc7A9`
- wstETH sur mainnet : `0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0`
- Flux Chainlink stETH/ETH : `0x86392dC19c0b719886221c78AB11eb8Cf5c52812`
- Pool Curve stETH : `0xDC24316b9AE028F1497c275EB9192a3Ea0f67022`
- Topic de l'événement `ReserveDataUpdated` (identique en v2 et v3) : `0x804c9b84...2897a`

## Points bloquants (à lire d'abord)

1. **wstETH n'est collatéral sur Aave v3 Arbitrum que depuis le 2023-03-01.** Le premier `ReserveDataUpdated` de la réserve est au bloc 65741201, le 2023-03-01 12:58 UTC. Si le backtest part de mars 2022, sa jambe « wstETH sur Aave Arbitrum » est anachronique sur un an. Pour cette période il faut un proxy explicite (Aave v3 Ethereum wstETH, ou Aave v2 stETH sur mainnet ; profondeur de ces deux voies NON VÉRIFIÉE).
2. **L'USDC natif n'est listé que depuis le 2023-06-28** (bloc 105770341, 12:14 UTC). Avant cette date, la seule réserve USDC est USDC.e, active depuis le 2022-03-16 (bloc 8001136). Aujourd'hui les deux taux divergent fortement : emprunt variable 9,08 % sur USDC.e contre 3,65 % sur l'USDC natif (valeurs `getReserveData` en APR, bloc 505556941). La TVL d'USDC.e est résiduelle (213 k$ selon DefiLlama). Il faut donc un raccord USDC.e → USDC natif, et ne jamais utiliser USDC.e après mi-2023.
3. **Aucune source gratuite ne sert l'historique complet du taux d'emprunt.**
   - DefiLlama `/chart` ne donne que le taux de dépôt ; l'endpoint emprunt `/chartLendBorrow` est payant.
   - L'API Aave remonte à un an au plus.
   - Aavescan exige une clé : l'API est dans le plan Advanced à 299 $/mois.
   - The Graph exige une clé (gratuite, 100 k requêtes/mois selon la recherche web, NON VÉRIFIÉE par appel).
   - Seule voie gratuite vérifiée : les événements on-chain.
4. **Les RPC publics Arbitrum ne sont pas des nœuds d'archive pour `eth_call`** (erreur `missing trie node` aux blocs 20M et 7M). Ils servent en revanche `eth_getLogs` sur tout l'historique, mais avec des limites 429 et des timeouts sur les plages denses.

## 1. Taux d'emprunt variable USDC sur Aave v3 Arbitrum

| Source | Endpoint | Profondeur vérifiée | Granularité | Trous / limites | Clé | Statut |
|---|---|---|---|---|---|---|
| Événements on-chain `ReserveDataUpdated` (topic1 = réserve) | `eth_getLogs` sur `https://arb1.arbitrum.io/rpc` | USDC.e : premier événement le 2022-03-16 (bloc 8001136). USDC natif : le 2023-06-28 (bloc 105770341). Dernier lu : bloc 505556751, varBorrow 3,6504 % APR. | Au bloc : un événement par interaction. Densité USDC.e en juin 2022 : 268 événements / 100 k blocs. USDC natif récent : 5514 événements / 1 M blocs. | 1 M blocs lus en 1,6 s sur une plage peu dense. 2 M blocs denses et 20 M blocs : `log query timed out`. Erreur 429 après quelques requêtes. Il faut un appel `getBlock` par bloc pour l'horodatage (ou une interpolation). Le taux lu est un APR en ray ; les index `liquidityIndex` et `variableBorrowIndex` sont aussi dans l'événement. | Non | VÉRIFIÉ |
| `getReserveData` historique (`eth_call` à un bloc passé) | même RPC | Dernier bloc seulement | n/a | `missing trie node` : pas d'archive | Non | VÉRIFIÉ (inutilisable) |
| API Aave v3 GraphQL, `borrowAPYHistory` | `POST https://api.v3.aave.com/graphql`, `request:{market, underlyingToken, window, chainId:42161}` | Fenêtre `LAST_YEAR` : 365 points, du 2025-09-16 au 2026-09-15. Fonctionne pour USDC natif et USDC.e. | `LAST_YEAR` : journalier. `LAST_DAY` : horaire (24 points). Fenêtres possibles : LAST_DAY, LAST_WEEK, LAST_MONTH, LAST_SIX_MONTHS, LAST_YEAR. | Pas plus d'un an. Champ `avgRate.value` : APR ou APY, NON VÉRIFIÉE. | Non | VÉRIFIÉ |
| Ancienne API `aave-api-v2.aave.com/data/rates-history` | redirige (301) vers `https://api.v3.aave.com/` | — | — | L'endpoint historique n'existe plus | — | VÉRIFIÉ (mort) |
| Subgraph Aave v3 Arbitrum (The Graph) | `https://gateway.thegraph.com/api/subgraphs/id/4xyasjQeREe7PxnF6wVdobZvCw5mhoHZq3T7guRpuNPf` | Non testable | NON VÉRIFIÉE | Sans clé : `auth error: missing authorization header`. L'ancien hébergé `api.thegraph.com` redirige vers une page d'erreur. | Oui | Accès VÉRIFIÉ, données NON VÉRIFIÉES |
| Aavescan | `GET https://api.aavescan.com/v2/csv?market=...&reserveAddress=...&apiKey=` | La doc annonce « entire history » en journalier ; NON VÉRIFIÉE | Doc : journalier, et horaire limité à un an | Sans clé : `401 Unauthorized, API key missing`. Page tarifs : Free = pas d'API, Pro 99 $ = CSV journalier, Advanced 299 $ = API et horaire. | Oui, payant | Accès VÉRIFIÉ, données NON VÉRIFIÉES |
| DefiLlama yields, dépôt | `https://yields.llama.fi/chart/d9fa8e14-0447-4207-9ae8-7810199dfa1f` (USDC natif) et `.../7aab7b0f-01c1-4467-bc0d-77826d870f19` (USDC.e) | USDC natif : du 2023-06-28 au 2026-09-15, 1176 points, sans trou. USDC.e : du 2022-08-08 au 2026-09-15, 1495 points, trou du 2023-11-04 au 2023-11-10. | Journalier | Taux de dépôt (`apyBase`) seulement, pas l'emprunt. USDC.e manque de mars à août 2022. | Non | VÉRIFIÉ |
| DefiLlama yields, emprunt | `https://yields.llama.fi/chartLendBorrow/{pool}` | — | — | Réponse : « Upgrade to the paid API plan » | Oui, payant | VÉRIFIÉ (payant) |

## 2. Proxy avant mars 2022 : taux d'emprunt USDC sur Aave v2 mainnet

| Source | Endpoint | Profondeur vérifiée | Granularité | Limites | Clé | Statut |
|---|---|---|---|---|---|---|
| Événements `ReserveDataUpdated` du LendingPool v2 | `eth_getLogs` sur `https://mainnet.gateway.tenderly.co` (même résultat sur `https://rpc.mevblocker.io`) | Événements présents dès le bloc 11400000 (2020-12-06) : varBorrow 3,672 %. 2021-03-08 : 45,33 %. 2022-03-16 : 3,006 %. 2022-06-21 : 2,007 %. 2026-09-03 : 11,21 % (4 événements / 10 k blocs, marché quasi éteint). Date de lancement exacte de v2 NON VÉRIFIÉE. | Au bloc (187 à 636 événements / 10 k blocs sur 2020-2022) | Sur ces deux RPC, la plage de 10 k blocs a répondu (limite maximale NON VÉRIFIÉE). Environ 300 appels suffisent de décembre 2020 à mars 2022. Refus d'autres RPC publics : publicnode (archive sur jeton), drpc (10 k blocs maximum sur le plan gratuit), 1rpc (50 blocs), cloudflare (800), blastapi (10), flashbots (`pruned history`). | Non | VÉRIFIÉ |
| API Aave v3 `borrowAPYHistory` avec le marché v2 (chainId 1) | idem section 1 | Liste vide | — | Ne couvre pas v2 | Non | VÉRIFIÉ (inutilisable) |
| DefiLlama yields | `https://yields.llama.fi/pools` | Aucun pool `aave-v2` dans la liste actuelle (projets présents : aave-v3, aave-v4). `/poolsOld` : payant (402). | — | — | — | VÉRIFIÉ (absent) |

## 3. Taux de dépôt wstETH sur Aave v3 Arbitrum

| Source | Endpoint | Profondeur vérifiée | Granularité | Limites | Clé | Statut |
|---|---|---|---|---|---|---|
| Événements on-chain | `eth_getLogs` sur `https://arb1.arbitrum.io/rpc`, topic1 = wstETH | Premier événement le 2023-03-01, bloc 65741201 : liq 0, varBorrow 0,25 %. Actuel (`getReserveData`) : dépôt 2,35e-7, soit ≈ 0 %. | Au bloc | Les mêmes limites de RPC qu'en section 1. **Rien avant mars 2023** (réserve non listée). | Non | VÉRIFIÉ |
| DefiLlama yields | `https://yields.llama.fi/chart/e62bcb01-ed4c-4ec9-8cfa-e86e7ccf7688` | Du 2023-03-01 au 2026-09-15, 1295 points, sans trou. Premier point `apyBase` 0,00696 %, dernier 0,00002 %. | Journalier | — | Non | VÉRIFIÉ |
| API Aave v3 `supplyAPYHistory` | idem section 1 | Un an : 365 points, du 2025-09-16 au 2026-09-15 | Journalier | Un an maximum | Non | VÉRIFIÉ |

## 4. APR de staking Lido

| Source | Endpoint | Profondeur vérifiée | Granularité | Limites | Clé | Statut |
|---|---|---|---|---|---|---|
| `stEthPerToken()` de wstETH, lu à un bloc historique (`eth_call` sur archive) | `https://mainnet.gateway.tenderly.co` (archive également servie par mevblocker, merkle.io, drpc) | Lu aux dates suivantes : 2021-02-19 (bloc 11888477) = 1,003748 ; 2021-05-24 = 1,022381 ; 2022-05-28 = 1,072808 ; 2022-06-21 = 1,075636 ; 2022-07-02 = 1,076922 ; 2023-04-07 = 1,117817 ; 2026-09-15 = 1,244100. APR dérivé du 28 mai au 2 juillet 2022 : ≈ 3,99 %. | Au choix (un appel par point, par exemple journalier) | Environ 2000 appels pour un point par jour depuis 2021. Date de déploiement de wstETH ≤ bloc 11888477 (antérieure NON VÉRIFIÉE). Le ratio est net des frais Lido de 10 %. | Non | VÉRIFIÉ, **voie la plus robuste** |
| Événement `TokenRebased` du contrat stETH | `eth_getLogs`, idem | Seulement vérifié récemment : rapport du 2026-09-15 12:00, elapsed 86400 s, APR dérivé 2,2972 %, identique à l'API Lido. Date de début (Lido V2) NON VÉRIFIÉE. | Par rapport oracle (journalier) | Avant V2, autre événement d'oracle (NON VÉRIFIÉ) | Non | Partiel |
| API Lido | `https://eth-api.lido.fi/v1/protocol/steth/apr/sma` et `/apr/last` | 7 points journaliers seulement (dernier : 2,298, SMA 2,269). `/apr/history` : 404. Le paramètre `?days=365` est ignoré. | Journalier | Aucun historique | Non | VÉRIFIÉ (pas d'historique) |
| DefiLlama yields, Lido | `https://yields.llama.fi/chart/747c1d2a-c668-4682-b9f9-296708a3dd90` | Du 2022-05-03 au 2026-09-15, 1597 points, sans trou | Journalier | Précision grossière en 2022 (valeurs 4 / 3,9 en juin 2022). Rien avant mai 2022. | Non | VÉRIFIÉ |

## 5. Ratio de marché stETH/ETH (décrochage de juin 2022)

| Source | Endpoint | Profondeur vérifiée | Granularité | Limites | Clé | Statut |
|---|---|---|---|---|---|---|
| Chainlink stETH/ETH, `getRoundData` lu au dernier bloc (pas besoin d'archive) | `https://ethereum-rpc.publicnode.com`, proxy `0x8639...2812` | Phase 1 (agrégateur `0x716B...0e1573`) : round 1 le 2021-08-25 19:04 (0,99898), round 1099 le 2024-09-06. Phase 2 (`0xC9c8...825c`) : round 1 le 2024-06-30, round 897 le 2026-09-15 (0,99972). **Plus bas de juin 2022 : 0,93502 le 2022-06-18 09:09 UTC** (round 304). | Heartbeat de 24 h, plus des rounds de déviation (deux le 13 juin) | Le creux intrajournalier est invisible : 31 rounds en juin 2022, écart médian de 24 h. Environ 2000 appels pour tout l'historique. | Non | VÉRIFIÉ |
| Curve `get_dy(1,0,1e18)` à un bloc historique | archive Tenderly, pool `0xDC24...7022` | Échantillons : 2022-05-31 = 0,98052 ; 2022-06-14 = 0,95489 ; **2022-06-15 22:43 = 0,93815** ; 2022-06-17 = 0,93910 ; 2022-06-19 = 0,93928 ; 2022-06-21 = 0,94085 ; 2022-07-02 = 0,96225 | Au bloc | Échantillonnage partiel ; le vrai minimum sur tout juin 2022 est NON VÉRIFIÉ | Non | VÉRIFIÉ (échantillon) |
| Événements Curve `TokenExchange` | `eth_getLogs` Tenderly, blocs 14970000 à 14979999 (15 et 16 juin 2022) | 622 échanges ; prix d'exécution le plus bas pour une vente de plus d'1 stETH : **0,93107** (bloc 14971186) | Par transaction | Inclut le glissement lié à la taille ; seuls 10 k blocs ont été scannés | Non | VÉRIFIÉ (échantillon) |
| DefiLlama coins | `https://coins.llama.fi/chart/ethereum:0xae7ab...,coingecko:ethereum?start=&span=&period=1d` ou `1h` | stETH remonte au moins au 2021-01-01. **Minimum journalier de juin 2022 : 0,93388 le 2022-06-18.** Minimum horaire du 16 au 19 juin : 0,90765 le 2022-06-17 01:01, douteux (voir limites). | 1d ou 1h ; 500 points maximum par requête | Deux séries de prix indépendantes appariées à l'heure : 61 paires sur 72, horodatages décalés. Le 0,908 horaire est probablement un artefact et doit être recoupé avec Curve. | Non | VÉRIFIÉ, fiabilité horaire douteuse |
| CoinGecko `market_chart/range` | `https://api.coingecko.com/api/v3/coins/staked-ether/market_chart/range` | Erreur 401, code 10012 : l'API publique est limitée aux 365 derniers jours | — | Payant pour 2022 | Oui | VÉRIFIÉ (inutilisable gratuitement) |

## Conséquences pour le backtest

- **Fenêtre « réelle » du montage exact** (wstETH et USDC natif sur Aave v3 Arbitrum) : **du 2023-06-28 à aujourd'hui.**
  - Du 2023-03-01 au 2023-06-28 : wstETH existe, emprunt sur USDC.e.
  - Du 2022-03-16 au 2023-03-01 : USDC.e existe, pas de wstETH. Le collatéral doit passer par un proxy (wstETH sur Aave mainnet, profondeur NON VÉRIFIÉE).
  - Avant 2022-03-16 : proxy Aave v2 mainnet pour USDC (vérifié depuis décembre 2020).
- **Raccords à documenter :**
  - v2 mainnet → USDC.e Arbitrum au 2022-03-16 (les deux séries se chevauchent sur mars à juin 2022 : mesurer l'écart).
  - USDC.e → USDC natif au 2023-06-28 ou plus tard. Choisir une date où la liquidité d'USDC natif est significative : le premier point DefiLlama montre une TVL de 17 k$.
- **Voie gratuite recommandée pour les taux : les événements `ReserveDataUpdated`.** Ils sont au bloc, exacts, et portent les index cumulés, ce qui permet de calculer l'intérêt réellement couru entre deux dates sans intégrer un APR.
  - Arbitrum : RPC public avec morceaux de 100 k à 250 k blocs, pauses d'environ 1 s et reprises sur 429 ou timeout. Horodatage par `getBlock` échantillonné.
  - Mainnet : morceaux de 10 k blocs sur Tenderly ou mevblocker.
  - Alternative payante : Aavescan Pro (99 $, CSV journalier) ou Advanced (299 $, API). The Graph avec clé gratuite reste à tester.
- **Recoupement :** l'API Aave (un an, journalier et horaire, gratuite) sert à valider l'extraction on-chain sur les 12 derniers mois.
- **Staking :** dériver l'APR de `stEthPerToken` lu sur archive (Tenderly), en journalier depuis février 2021. Recouper avec `TokenRebased` (récent) et DefiLlama (depuis mai 2022). Ne pas utiliser l'API Lido pour l'historique.
- **Taux de dépôt wstETH sur Arbitrum :** ≈ 0 sur toute la période vérifiée (2023-03 → aujourd'hui) ; le modéliser à 0 est défendable. Avant mars 2023, sans objet.
- **Stress de décrochage stETH/ETH :** base Chainlink en journalier, creux 0,935 le 2022-06-18. Le creux intrajournalier (exécution Curve 0,931, voire plus bas) exige un scan complet des `TokenExchange` ou des `get_dy` horaires sur la période du 10 au 30 juin 2022, soit environ 150 k blocs donc 15 appels de logs : c'est à faire. Le 0,908 DefiLlama horaire n'est pas fiable tel quel.
- **Manques :**
  - Le taux d'emprunt USDC.e de mars à août 2022 n'existe que via les logs (DefiLlama ne l'a pas).
  - L'historique `TokenRebased` d'avant Lido V2, et la profondeur de wstETH sur Aave v3 mainnet (proxy collatéral), sont NON VÉRIFIÉS.
  - Nature APR ou APY de `avgRate` dans l'API Aave : NON VÉRIFIÉE.
  - Les taux lus on-chain sont des APR (ray, composition par seconde) ; la conversion est à faire explicitement.
