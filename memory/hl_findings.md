# Hyperliquid — comportements vérifiés en conditions réelles

Pendant du fichier `aave_findings.md`, pour la place Hyperliquid. Tout ce qui
suit a été constaté le 2026-09-02 sur le compte réel
`0x4F7ed211FcEF5555B0EC309E3bFfcCfE27750C89`, pas déduit de la documentation.

Ces découvertes contraignent `src/delta0/hl_executor.py` et
`src/delta0/venues/bridge.py`. Ne pas les "simplifier" sans relire ce fichier.

## 1. Compte unifié ou compte historique : deux modèles, pas un

Hyperliquid expose un compte **spot** et un compte **perp**, mais ce que ça
signifie dépend du modèle sous lequel le compte tourne. Le nôtre est passé en
**compte unifié** le 2026-09-02, en cours de session.

**Compte unifié (le nôtre aujourd'hui).** Il n'y a qu'un seul solde : l'USDC
spot *est* le collatéral qui sert de marge aux positions perp.
`marginSummary.accountValue` ne rapporte que l'équité des positions ouvertes —
il vaut 0 quand il n'y a pas de position, même avec 29,8 USDC disponibles. Le
marqueur fiable est `tokenToAvailableAfterMaintenance` dans `spot_user_state`.
Le transfert spot↔perp est **désactivé** :

```
{'status': 'err', 'response': 'Action disabled when unified account is active'}
```

**Compte historique.** Deux poches séparées, et chaque opération du bot en
touche une différente : dépôt depuis Arbitrum → spot ; retrait
(`withdraw_from_bridge`, action `withdraw3`) → perp ; marge d'un ordre → perp.
Sans rééquilibrage, chaque aller-retour de pont vide le perp de son montant.

Le code doit gérer les deux : `round_trip` tente le transfert et traite le
refus « unified account » comme une issue normale, pas comme une erreur.

Attention au piège de diagnostic : voir `accountValue` à 0 avec des fonds bien
présents ne veut **pas** dire que l'argent est mal placé. Sur un compte unifié
c'est la lecture normale. Lire les deux avant de conclure —
`info.user_state(addr)` et `info.spot_user_state(addr)` — et
`info.user_non_funding_ledger_updates(addr, depuis_ms)` pour le journal des
mouvements.

## 1 bis. Le SDK ne lève pas, il retourne une enveloppe d'erreur

C'est le piège le plus dangereux de cette place, parce qu'il est silencieux.
Toutes les actions — poser un ordre, retirer du pont, transférer entre
sous-comptes — signalent un refus **par valeur de retour** :

```
{'status': 'err', 'response': '...'}
```

Du code écrit autour d'un `try/except` prend donc un refus pour un succès.
Constaté trois fois dans notre propre code :

- le transfert spot→perp refusé, journalisé comme effectué ;
- `bridge_in` : un retrait refusé aurait marqué l'intent `confirmed` et
  enregistré une latence `bridge_in_submit` fictive ;
- `hl_post_only_cancel` : un ordre rejeté faisait renvoyer `None` par
  `_extract_order_id`, l'annulation était sautée, et l'opération journalisée
  comme confirmée — **avec un échantillon de latence P1/P2 pour un ordre qui
  n'a jamais existé**.

Un test unitaire assertait même explicitement ce dernier comportement
(« toujours confirmé puisqu'aucune exception n'a été levée »). Il encodait le
bug comme une intention.

Tout passe désormais par `delta0.hl_api.ensure_ok`, qui transforme un refus en
exception au point d'appel. Une réponse de forme inconnue compte comme un
refus : le SDK renvoie toujours un dictionnaire pour ces actions, donc une
autre forme signifie que le contrat a changé, et la lecture prudente d'une
réponse illisible quand des fonds ont bougé est l'échec.

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
