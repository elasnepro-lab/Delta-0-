# Backtest : sources de données

Inventaire fait le 2026-09-16, avant d'écrire une ligne du backtest (phase 7 du
plan). Chaque valeur a été lue par un appel réel, sans clé ni `.env` ; ce qui ne
se vérifie pas par un appel est marqué NON VÉRIFIÉE. Les scripts et sorties
brutes cités dans les rapports sont restés dans l'espace de travail de la
session et ne sont pas versés : les rapports portent les endpoints exacts pour
rejouer chaque lecture.

| Rapport | Question |
|---|---|
| [verification-oracle-wsteth.md](verification-oracle-wsteth.md) | De quoi dépend le prix du wstETH qui peut nous liquider sur Aave Arbitrum |
| [inventaire-prix-funding.md](inventaire-prix-funding.md) | Prix ETH Binance et Hyperliquid, funding des deux places |
| [inventaire-taux-staking.md](inventaire-taux-staking.md) | Taux d'emprunt USDC, rendement Lido, ratio de marché stETH/ETH |

## Ce qui change la méthode du README §15.3

1. **Le décrochage stETH/ETH ne liquide pas.** Depuis le 2023-06-26, l'oracle
   Aave du wstETH vaut taux de conversion × ETH/USD Chainlink, recalculé au wei
   près. Seule une baisse du taux (slashing Lido) touche le facteur de santé, et
   le short ETH ne la couvre pas : c'est un scénario de stress à part.
2. **Le montage exact n'existe que depuis le 2023-06-28** : wstETH collatéral sur
   Aave Arbitrum depuis le 2023-03-01, USDC natif depuis le 2023-06-28 (USDC.e
   avant, dont le taux diverge). 2021 et 2022 ne se rejouent qu'en PROXY.
3. **Aucun prix mark Hyperliquid historique n'est public.** Le mark Binance
   futures à la minute sert de proxy, avec un choc d'écart réglable.
4. **Le taux d'emprunt historique n'est gratuit que par les événements
   `ReserveDataUpdated`** lus on-chain ; le subgraph demande une clé et Aavescan
   est payant.

## Décisions de l'opérateur, 2026-09-16

Reportées au README §15.3, qui fait foi :

| Question | Décision |
|---|---|
| Granularité | 1 minute sur toute la période, archives Binance |
| Période | 2021 → aujourd'hui en trois segments : FIDÈLE depuis 2023-06-28, INTERMÉDIAIRE 2023-03-01 → 2023-06-28, PROXY avant |
| Prix de liquidation du short | Mark futures Binance à la minute plus un choc d'écart à la hausse réglable, testé sur une plage |
| Taux d'emprunt | Événements `ReserveDataUpdated` on-chain, gratuits, recoupés avec l'API Aave sur douze mois |

La méthode sera figée avant que le backtest rende un chiffre retenu, puis soumise
à `/revue-finance` en session vierge.

## Une lecture à corriger dans `inventaire-prix-funding.md`

La conclusion 3 du rapport dit qu'un proxy Binance « sous-estime le risque de
liquidation d'environ la moitié de la marge », en s'appuyant sur le plus bas de
Hyperliquid le 2025-10-10, 4,7 % sous Binance. Le sens est inversé pour notre
montage : **le short se liquide à la hausse**, et une mèche vers le bas l'enrichit.
L'écart qui compte est celui des plus HAUTS entre Hyperliquid et Binance pendant
un squeeze, et il reste à mesurer. Le constat de fond tient : les deux places
divergent pendant les cascades, et le choc d'écart doit être calibré, pas supposé nul.
