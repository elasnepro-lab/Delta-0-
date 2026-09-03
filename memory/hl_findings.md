# Hyperliquid — comportements vérifiés en conditions réelles

Pendant du fichier `aave_findings.md`, pour la place Hyperliquid. Tout ce qui
suit a été constaté le 2026-09-02 sur le compte réel
`0x4F7ed211FcEF5555B0EC309E3bFfcCfE27750C89`, pas déduit de la documentation.

Ces découvertes contraignent `src/delta0/hl_executor.py` et
`src/delta0/venues/bridge.py`. Ne pas les "simplifier" sans relire ce fichier.

## 1. Deux sous-comptes, et ils ne se parlent pas tout seuls

Hyperliquid sépare un compte **spot** et un compte **perp**. Le piège est que
chaque opération du bot touche un sous-compte différent :

| Opération | Sous-compte |
|---|---|
| dépôt depuis Arbitrum (`bridge_out` vers Bridge2) | **spot** |
| retrait vers Arbitrum (`withdraw_from_bridge`, action `withdraw3`) | **perp** |
| ordre perp (post-only ALO, et toute la stratégie) | marge sur le **perp** |

Conséquences vécues :

- `wait_for_hl_credit` lisait `user_state(...)["marginSummary"]["accountValue"]`
  — le perp seul — et n'a jamais vu un crédit arrivé sur le spot. La première
  traversée réelle a tourné jusqu'à son timeout de 900 s, l'argent bien visible
  sur l'autre sous-compte. Le solde HL doit se lire comme **spot + perp**.
- Un aller-retour de pont déplace donc le solde du perp vers le spot, sans
  retour. Sur les 14 traversées d'une marche à blanc de 7 jours : −70 USDC sur
  le perp. Le retrait casse vers la cinquième traversée, et les ordres avec.
  `round_trip` intercale désormais `usd_class_transfer(montant, to_perp=True)`
  après le crédit.

Lire les deux : `info.user_state(addr)` (perp) et `info.spot_user_state(addr)`
(spot, liste `balances` où chercher `coin == "USDC"`).
`info.user_non_funding_ledger_updates(addr, depuis_ms)` donne le journal des
mouvements — indispensable pour comprendre où est passé un solde.

## 2. Les ordres doivent tomber pile sur les grilles de la place

Le SDK refuse **localement**, avant tout appel réseau :

```
ValueError: ('float_to_wire causes rounding', 0.00501850573991594)
```

Deux règles distinctes :

- **taille** : au plus `szDecimals` décimales. Valeur par asset, lue dans
  `info.meta()["universe"]`. ETH = 4.
- **prix** : au plus 5 chiffres significatifs **et** au plus
  `6 - szDecimals` décimales, la plus contraignante des deux gagnant. Un prix
  entier est toujours accepté.

Implémentation : `hl_executor.round_size` / `round_price`. La précision est lue
sur la place et mise en cache dans le câblage CLI, jamais codée en dur.

Attention à l'interaction avec le plancher de notionnel : HL impose 10 $
minimum par ordre, et arrondir la taille vers le bas peut faire passer dessous.
Le code remonte alors d'un cran de grille.

## 3. Construire le client SDK coûte deux allers-retours HTTP

`Exchange.__init__` construit un `Info`, qui va chercher les métadonnées perp
et spot en HTTP. Le faire à chaque ordre plaçait ces deux appels **dans la
fenêtre chronométrée** : premier ordre réel mesuré à 2 609 ms pour un budget
P1/P2 de 2 000 ms. Client construit une fois → 1 811 ms.

Au-delà de la mesure faussée : P1/P2 est le chemin de réponse à une
liquidation. Monter un client HTTP au milieu d'une urgence est exactement ce
que ce budget existe pour empêcher.

## 4. Lecture du chiffre P1/P2

Le traceur chronomètre **poser + annuler**, soit deux allers-retours, alors que
la vraie action P1/P2 en urgence n'en fait qu'**un**. La mesure majore donc le
chemin réel d'environ un facteur deux. Un `DEPASSE` sur P1/P2 dans le rapport
final doit se lire avec ça en tête avant de conclure quoi que ce soit.

## 5. Minimums de la place

| Contrainte | Valeur |
|---|---|
| dépôt via le pont Arbitrum | 5 USDC (en dessous : perdus) |
| retrait vers Arbitrum | 2 USDC, frais 1 USDC par retrait |
| notionnel par ordre | 10 $ |

Contrat Bridge2 sur Arbitrum : `0x2Df1c51E09aECF9cacB7bc98cB1742757f163dF7`.
USDC natif uniquement (`0xaf88d065e77c8cC2239327C5EDb3A432268e5831`), jamais
USDC.e.
