# Vérification on-chain : valorisation du wstETH par Aave v3 Arbitrum

Date de l'audit : 2026-09-16 (horodatage chaîne 2026-09-15 22:12 à 22:18 UTC).
RPC : `https://arb1.arbitrum.io/rpc` (chainId 42161) ; mainnet `https://ethereum-rpc.publicnode.com` (chainId 1).
Pas de clé, pas de .env. Scripts et sorties brutes dans ce dossier : `oracle_probe1.py`, `oracle_probe2.py`, `oracle_hist.py`, `oracle_logs.py`, `oracle_mainnet.py`, `oracle_misc.py`, `oracle_rounds.py` et les fichiers `oracle_out*.txt`.

## Conclusion

**Aujourd'hui, et depuis le 2023-06-26, un décrochage du prix de marché stETH/ETH ne change PAS le prix oracle du wstETH sur Aave v3 Arbitrum.**
Le prix est calculé ainsi :
`prix wstETH (USD, 8 déc.) = min(taux de conversion wstETH→stETH, plafond CAPO) × ETH/USD Chainlink`.
Le taux de conversion est `stEthPerToken()` du contrat wstETH sur mainnet, relayé par un flux Chainlink « wstETH-stETH Exchange Rate ». La seule composante de marché est ETH/USD.

L'historique se découpe ainsi :
- **Juin 2022** : la question ne se pose pas. wstETH n'était pas encore listé sur Aave v3 Arbitrum ; il l'a été le 2023-03-01.
- **2023-03-01 → 2023-06-26** : les deux premières sources ÉTAIENT sensibles au marché.

## 1. Adresses (confirmées par appel, bloc 505556953)

| Élément | Adresse | Preuve |
|---|---|---|
| PoolAddressesProvider | 0xa97684ead0e402dC232d5A977953DF7ECBaB3CDb | `getMarketId()` = "Arbitrum Aave Market" |
| AaveOracle | 0xb56c2f0b653b2e0b10c9b928c8580ac5df02c7c7 | `PAP.getPriceOracle()` ; confirme l'adresse vue dans les journaux du bot |
| Pool | 0x794a61358d6845594f94dc1db02a252b5b4814ad | `PAP.getPool()` |
| PoolConfigurator | 0x8145edddf43f50276641b55bd3ad95944510021e | `PAP.getPoolConfigurator()` |
| wstETH (Arbitrum) | 0x5979D7b546E38E414F7E9822514be443A4800529 | a une source dans l'oracle |
| WETH (Arbitrum) | 0x82aF49447D8a07e3bd95BD0d56f35241523fBab1 | idem |
| USDC natif | 0xaf88d065e77c8cC2239327C5EDb3A432268e5831 | idem (adresse aussi présente dans `src/delta0/tracer.py`) |

Paramètres de l'oracle lus par appel :
- `BASE_CURRENCY()` = 0x0 et `BASE_CURRENCY_UNIT()` = 1e8 : les prix sont en USD avec 8 décimales.
- `getFallbackOracle()` = 0x0 : aucun oracle de repli.
- `PAP.getPriceOracleSentinel()` = 0x0 : **aucune période de grâce après une panne du séquenceur sur ce marché.**

`getSourceOfAsset` / `getAssetPrice` au bloc 505556953 :

| Actif | Source | getAssetPrice |
|---|---|---|
| wstETH | 0xb4a28df1b926646f94e6fe6f15828c491b4def5f | 297199303957 (2971,99 $) |
| WETH | 0xbd41b1548a5a06544cbcf87c0c54864312842c00 | 238887064932 (2388,87 $) |
| USDC | 0xb0c9a7122aab68f75cffd9851e867144dbff113b | 99990271 (0,9999 $) |
| USDC.e | 0xb0c9a7122aab68f75cffd9851e867144dbff113b | 99990271 |

## 2. La source actuelle du wstETH : un adaptateur CAPO

Contrat 0xb4a28DF1b926646f94e6fE6f15828c491b4def5F (bloc 505557068) :
- `description()` = "Capped wstETH / stETH(ETH) / USD" ; `decimals()` = 8
- `BASE_TO_USD_AGGREGATOR()` = 0xbd41b1548a5a06544cbcf87c0c54864312842c00. C'est la même source que WETH.
- `RATIO_PROVIDER()` = 0xb1552c5e96b312d0bf8b554186f846c40614a540 ; `RATIO_DECIMALS()` = 18
- `getRatio()` = 1244099608498421563
- `getSnapshotRatio()` = 1227259738293014024, relevé le 1771345811 (2026-02-17)
- `getMaxYearlyGrowthRatePercent()` = 968, soit 9,68 %/an ; `getMaxRatioGrowthPerSecond()` = 3767083417
- `MINIMUM_SNAPSHOT_DELAY()` = 604800 s (7 jours)
- `isCapped()` = false
- `ACL_MANAGER()` = 0xa72636cbcaa8f5ff95b2cc47f3cdee83f3294a0b

Les dépendances :
- **RATIO_PROVIDER** 0xB1552C5e… est un proxy Chainlink.
  - `description()` = "wstETH-stETH Exchange Rate" ; `decimals()` = 18
  - `aggregator()` = 0x9EC305Ef… (`typeAndVersion()` = "AccessControlledOCR2Aggregator 1.0.0")
  - `latestAnswer()` = 1244099608498421563
  - **Sur mainnet, au bloc 25985780, `stEthPerToken()` du wstETH L1 (0x7f39C581F595B53c5cb19bD0b3f8dA6c935E2Ca0) = 1244099608498421563. La valeur est identique au wei près.**
  - C'est donc bien le taux de conversion du jeton, et non un prix de marché.
- **BASE_TO_USD_AGGREGATOR** 0xbD41b154… est un proxy Chainlink.
  - `description()` = "ETH / USD" ; `decimals()` = 8 ; `phaseId()` = 2
  - `aggregator()` = 0xa5E1a369… (`typeAndVersion()` = "DualAggregator 1.0.0", `minAnswer()` = 1, `maxAnswer()` = très grand, donc aucune borne utile)

À titre de contraste, le marché : sur mainnet, le flux Chainlink 0x86392dC19c0b719886221c78AB11eb8Cf5c52812 (`description()` = "STETH / ETH") renvoie `latestAnswer()` = 0,999717336525921. L'adaptateur Aave ne lit pas ce flux.

### Vérification numérique (bloc 505558342)

- `getAssetPrice(wstETH)` = 296345077788
- ETH/USD `latestAnswer()` = 238200442926
- ratio = 1244099608498421563
- ratio × ETH/USD / 1e18 = 296345077788. **Écart : 0 unité.**

### Le plafond CAPO

Le plafond vaut `snapshotRatio + growthPerSecond × (t − snapshotTs)`, soit 1,295688 au bloc 505558342. Le ratio actuel est à 96,02 % du plafond, ce qui laisse 4,15 % de marge. Le plafond ne borne que la **hausse** du ratio : c'est une protection contre la manipulation. Une baisse du ratio, par exemple un slashing Lido, passe intégralement dans le prix.

## 3. De quoi dépend le prix du wstETH : réponse à la question centrale

| Facteur | Effet sur le prix oracle actuel |
|---|---|
| ETH/USD (Chainlink) | Oui, linéaire |
| Taux de conversion wstETH→stETH (`stEthPerToken`, relayé depuis mainnet) | Oui : +~9,7 %/an au plus au-delà du plafond ; baisse non bornée (slashing) |
| Prix de marché stETH/ETH ou wstETH/ETH (DEX, CEX) | **Non** |

Un « décrochage façon juin 2022 » ne fait donc bouger ni le LTV ni le facteur de santé (HF) de la position Aave, et ne déclenche pas de liquidation.

Côté taux de conversion, la cadence mesurée sur 40 rounds (2026-08-15 → 2026-09-15) :
- une mise à jour toutes les ~86 400 s (écart médian 86 390 s, maximum 86 422 s) ;
- une variation par mise à jour de ~0,006 % (maximum 0,0065 %).

Le ratio est donc une marche quotidienne quasi linéaire. En pratique, le seul chemin par lequel un décrochage atteint la position est un slashing ou une perte réelle au niveau du protocole Lido, qui ferait baisser `stEthPerToken`. La décote de marché, elle, n'y passe pas. Un décrochage ne compte qu'au moment de sortir : vendre du wstETH sur le marché pour rembourser, et non se faire liquider.

## 4. Historique des sources du wstETH

Méthode : lecture des logs `AssetSourceUpdated(address,address)` (topic0 0x22c5b7b2…) de l'AaveOracle.
- Plage balayée : du bloc 0 au bloc 505557506, par tranches de 50 M de blocs, sans erreur ni troncature.
- 67 événements au total, dont 5 pour wstETH.
- Recoupement : l'événement `ReserveInitialized` du PoolConfigurator filtré sur wstETH (balayé jusqu'au bloc 505558342) sort un unique événement au bloc 65735133, le **2023-03-01 12:32 UTC**, dans la même transaction que la première source (tx 0x8b90c2fd…eaef2).
- Le RPC public n'est pas une archive complète : `getReservesList()` aux blocs 15 000 000 (juin 2022, avant Nitro), 23 000 000 et 65 735 132 échoue avec « missing trie node ». L'absence de wstETH en juin 2022 repose donc sur les logs, qui sont complets depuis le bloc 0, et non sur un état lu à cette date.

Chaque source ci-dessous a été inspectée au bloc courant (getters immuables ; signatures retrouvées à partir des sélecteurs du bytecode). Descriptions des flux intermédiaires lues par appel.

| # | Période active | Source | Construction (lue on-chain) | Sensible au marché stETH ? |
|---|---|---|---|---|
| 1 | 2023-03-01 → 2023-06-05 | 0x230e0321cf38f09e247e50afc7801ea2351fe56f | `name()` = "wstETH/stETH/USD" ; ASSET_TO_PEG = 0xb1552c5e… (wstETH-stETH Exchange Rate) × PEG_TO_BASE = 0x07C5b924… (`description()` = "STETH / USD") | **OUI** : STETH/USD est un prix de marché |
| 2 | 2023-06-05 → 2023-06-26 | 0x3105c276558dd4cf7e7be71d73be8d33bd18f211 | `name()` = "wstETH/ETH/USD" ; ASSET_TO_PEG = 0xb523AE26… (`description()` = "WSTETH / ETH", `latestAnswer()` = 1,243862562 ≠ taux 1,244099608) × PEG_TO_BASE = 0x639fe6ab… (ETH / USD) | **OUI** : WSTETH/ETH est un prix de marché |
| 3 | 2023-06-26 → 2024-03-18 | 0x945fd405773973d286de54e44649cc0d9e264f78 | `description()` = "wstETH/ETH/USD" ; ASSET_TO_PEG = 0xb1552c5e… (Exchange Rate) × PEG_TO_BASE = 0x639fe6ab… (ETH / USD) | Non |
| 4 | 2024-03-18 → 2026-03-29 | 0x87fe1503befbf98c35c7526b0c488d950f822c0f | CAPO "Capped wstETH / stETH(ETH) / USD" ; RATIO_PROVIDER = 0xb1552c5e… ; BASE_TO_USD = 0x639fe6ab… ; 9,68 %/an | Non |
| 5 | depuis 2026-03-29 | 0xb4a28df1b926646f94e6fe6f15828c491b4def5f | CAPO identique ; BASE_TO_USD = 0xbd41b154… (ETH / USD, DualAggregator) | Non |

Dates et transactions des changements :
- Blocs 65735133, 98045668, 104943290, 191632097 et 446795767.
- Transactions 0x8b90c2fd…, 0xb55fc46c…, 0xb4d08ea5…, 0x2dfa52a3…, 0x574b2f64….
- Le détail figure dans `oracle_out_logs.txt`.

Ce que dit l'annuaire de référence Chainlink (source documentaire, non on-chain) :
- 0x07C5b924 « STETH / USD » : `attributeType` = "cex_price", déviation 0,3 %, heartbeat 86 400 s.
- 0xb523AE26 « WSTETH / ETH » : `attributeType` = "dex_state_price", déviation 0,5 %, heartbeat 86 400 s.

Ces deux lignes confirment la nature marché de ces flux.

**Pour juin 2022 :** wstETH n'existait pas sur Aave v3 Arbitrum. Aucun oracle Aave Arbitrum n'a donc valorisé du wstETH pendant ce décrochage. Si l'on transpose le montage à juin 2022 avec la règle actuelle, le prix oracle vaut taux × ETH/USD, et la décote de ~5-7 % n'entre pas dans le HF. Si l'on voulait reproduire les règles d'époque des premiers mois de listing (sources 1 et 2), elles l'auraient fait entrer.

## 5. ETH/USD côté Aave

**Source WETH actuelle et base de l'adaptateur wstETH :**
- Proxy 0xbD41b1548a5A06544cBcf87c0c54864312842C00 ("ETH / USD", 8 décimales).
- Agrégateur 0xa5E1a36938769cbd5a26f5e19D8FCB379f597c83 ("DualAggregator 1.0.0").
- Actif depuis le 2026-03-29. Avant, c'était le proxy 0x639Fe6ab55C921f74e7fac1ee960C0B6293ba612, agrégateur 0xD827123D014578C965F6c9d87A641ec05FaA5501, du 2022-03-11 au 2026-03-29.

**Seuil de déviation et heartbeat : NON VÉRIFIÉS on-chain.** Ils ne sont exposés par aucune fonction du contrat. Trois sources d'information :
- *Annuaire Chainlink* (`https://reference-data-directory.vercel.app/feeds-ethereum-mainnet-arbitrum-1.json`, lu le 2026-09-15) :
  - L'agrégateur 0xa5E1… y figure sous un autre proxy (0xAfF2135E3CE17578929C0ab714e2923f0C40b0DC), avec un seuil de 0,05 % et un heartbeat de 86 400 s.
  - Le proxy 0xbD41… n'y figure pas.
  - L'ancien proxy 0x639Fe6… y figure avec un seuil de 0,05 % et un heartbeat de 1 755 s. Cette valeur de heartbeat est atypique et reste NON VÉRIFIÉE.
- *Mesure on-chain* (150 rounds lus via `getRoundData`, du 2026-09-15 18:56 au 22:17 UTC) :
  - Écart entre mises à jour : minimum 29 s, médiane 60 s, maximum 511 s.
  - Variation entre mises à jour : minimum 0,0501 %, médiane 0,079 %, maximum 0,458 %.
  - Ces chiffres collent avec un seuil de 0,05 %. La fenêtre de 3,3 h est trop courte pour observer le heartbeat.
- *Recommandation backtest* : modéliser le prix oracle ETH comme un prix de marché échantillonné, mis à jour dès que l'écart dépasse 0,05 %. Pour Aave, ce retard est négligeable face à des bougies 1 min.

## 6. USDC côté Aave

**Source actuelle** 0xB0C9A7122aaB68F75CffD9851E867144DBFF113b, active depuis le 2026-03-29 et commune à USDC et USDC.e :
- `description()` = "Capped USDC/USD", `decimals()` = 8
- `ASSET_TO_USD_AGGREGATOR()` = 0xDbFF913E9058C1E60446150D23Bb0fFE9144d531 ("USDC / USD", agrégateur 0x6Baf4dBC…, "DualAggregator 1.0.0", `maxAnswer()` = 105000000)
- `getPriceCap()` = 104000000, soit un plafond à 1,04 $
- `isCapped()` = false
- `latestAnswer()` = 99990271

Le prix USDC n'est **pas fixe**. C'est le prix de marché Chainlink USDC/USD, plafonné à 1,04 $ et sans plancher : un dépeg à la baisse passe dans le prix. Pour la dette USDC, une baisse de l'USDC réduit la valeur de la dette et améliore donc le HF.

Cadence mesurée sur 27 rounds : une mise à jour toutes les ~86 400 s, variation maximale 0,0076 %. L'annuaire indique pour l'agrégateur 0x6Baf4… un seuil de 0,1 % et un heartbeat de 86 400 s (documentaire).

**Historique USDC :**
- Chainlink USDC/USD brut, proxy 0x50834F3163758fcC1Df9973b6e91f0F0F0434aD3 : pour USDC.e dès le 2022-03-11, pour USDC natif dès le 2023-06-28.
- Puis « Capped USDC/USD » 0xDe25a88F… à partir du 2024-03-18.
- Puis « Capped USDC/USD » 0x6200A512…, `getPriceCap()` = 1,04, à partir du 2025-06-02.
- Puis la source actuelle à partir du 2026-03-29.

## 7. Conséquences pour le backtest

1. **Prix de collatéral Aave** : `P_oracle(wstETH) = R(t) × ETH/USD(t)`.
   - R(t) est le taux `stEthPerToken`, historique quotidien disponible sur mainnet (lecture de `stEthPerToken` par bloc, ou flux Exchange Rate).
   - ETH/USD suit le prix de marché, par pas de 0,05 %.
   - N'utiliser **ni** le prix de marché wstETH **ni** stETH/ETH pour le LTV ou le HF.
   - Le plafond CAPO (9,68 %/an au-dessus d'un relevé hebdomadaire) peut être ignoré : le rendement Lido est bien inférieur, et la marge est aujourd'hui de 4,15 %.
2. **Liquidation** : `HF = R × ETH/USD × Q_wstETH × LT(0,79) / (dette_USDC × P_USDC)`. Le décrochage stETH n'y figure pas.
   - Le couvert (short) côté HL est en ETH : la couverture porte donc sur ETH/USD × R, la dérive de R étant de ~+3 %/an à suivre. Le risque de base marché stETH/ETH ne se matérialise qu'**à la sortie**, en cas de vente du wstETH.
   - Scénario de stress à modéliser à part : slashing Lido, c'est-à-dire une chute de R, qui baisse directement le HF et n'est pas couverte par le short ETH.
3. **Scénario juin 2022** : l'appliquer avec la règle actuelle (taux × ETH/USD), en indiquant que wstETH n'était pas listé à l'époque. Ajouter une variante « règle 2023-03 → 2023-06 » seulement si l'on veut un stress plus sévère que la réalité actuelle. Dans cette variante, la décote stETH entre dans le HF.
4. **Panne du séquenceur Arbitrum** : pas de PriceOracleSentinel (adresse 0x0 lue), donc pas de période de grâce. Des liquidations sont possibles dès la reprise, sur un ETH/USD rattrapé d'un coup.
5. **Dette USDC** : utiliser USDC/USD Chainlink, plafonné à 1,04 $, et non 1,00 fixe. L'effet est de second ordre.

## Valeurs NON VÉRIFIÉES

- Heartbeat et seuil de déviation de tous les flux Chainlink : aucune fonction on-chain ne les expose. Les valeurs données viennent de l'annuaire documentaire Chainlink, recoupé par la cadence mesurée pour ETH/USD (seuil ≈ 0,05 %).
- Mécanique hors chaîne du relais du taux wstETH-stETH par Chainlink (fréquence garantie, comportement en cas de panne) : seules l'égalité avec mainnet à l'instant t et la cadence quotidienne observée sont vérifiées.
- État des réserves en juin 2022 lu directement (le RPC public n'a pas l'état archivé) : conclusion tirée des logs complets.
