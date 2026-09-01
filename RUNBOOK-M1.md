# Runbook M1 — marche à blanc (TRACER)

Complément opérationnel du README §14. Le README reste la source de vérité :
si ce fichier le contredit, le README gagne.

Critère de sortie M1 : **7 jours de DRY_RUN, rapport p50/p95 des 5 chemins
critiques + journal des tirs à blanc.** Le rapport est produit par
`delta0 report` ; M1 est fermé quand chaque chemin y est `OK`.

## 0. Pré-requis machine

```bash
uv sync                      # venv Python 3.12 + dépendances
cp config.yaml.example config.yaml
cp .env.example .env         # puis remplir BOT_MASTER_* (voir §3)
uv run pytest -q             # doit être vert avant toute session live
```

Wallet opérateur : `0x4F7ed211FcEF5555B0EC309E3bFfcCfE27750C89`.
Float attendue : ~50 USDC + ~0,005 ETH de gas sur Arbitrum.

## 1. Étape A — observation seule (aucune transaction)

C'est le mode par défaut : `config.tracer.dry_run: true`, pas de
`--live-micro-ops`. Aucune clé privée n'est chargée.

```bash
uv run delta0 config-check
uv run delta0 status                       # recette lecture seule (M0)
uv run delta0 tracer --duration 7d --cadence 5
```

Au démarrage le tracer : ouvre le WS Hyperliquid, réconcilie le journal
SQLite contre l'état on-chain (README §13), puis boucle
`snapshot -> decide -> journalise`. Il n'exécute rien.

Ce que l'étape A mesure : `snapshot` et `decision`. Elle ne remplit **aucun**
chemin critique — le rapport affichera `AUCUN` partout. C'est normal.

## 2. Vérifier l'ABI Aave sur fork avant toute tx réelle

Zéro dollar en jeu, à refaire après toute modification du code Aave.

```bash
anvil --fork-url https://arb1.arbitrum.io/rpc --port 8545 --chain-id 42161
uv run python scripts/precheck_aave_fork.py
```

Critère : 6 transactions `status=1` et solde USDC final == initial.
Découvertes contractuelles : `memory/aave_findings.md`.

## 3. Étape B — micro-ops réelles (c'est ici que l'argent bouge)

Trois verrous doivent tomber ensemble, sinon le bot refuse de démarrer :

1. `config.yaml` : `tracer.dry_run: false`
2. `.env` : `BOT_MASTER_PRIVATE_KEY` renseignée (ni vide, ni `REPLACE...`)
3. CLI : `--live-micro-ops` **et** un `--confirm <op_kind>` par opération

```bash
uv run delta0 tracer --live-micro-ops \
  --confirm aave_approve --confirm aave_supply --confirm aave_borrow \
  --confirm aave_repay --confirm aave_withdraw \
  --duration 60s --cadence 30
```

Premier cycle Aave dans les 30 premières secondes, ~0,05 $ de gas, le solde
USDC revient à son état initial. Vérifier ce round-trip **avant** de lancer
les 7 jours.

Puis ajouter HL et le pont, et lancer la session longue :

```bash
uv run delta0 tracer --live-micro-ops \
  --confirm aave_approve --confirm aave_supply --confirm aave_borrow \
  --confirm aave_repay --confirm aave_withdraw \
  --confirm hl_post_only_cancel --confirm bridge_out --confirm bridge_in \
  --duration 7d --cadence 5
```

Cadences des micro-ops (dans `config.yaml`, section `tracer`) :
Aave toutes les 30 min, HL toutes les 10 min, pont toutes les 12 h.
Sur 7 jours : ~336 cycles Aave, ~1000 aller-retours HL, ~14 traversées de pont.

Arrêt propre à tout moment : créer un fichier `KILL` à la racine. Le guard
refuse alors toute nouvelle micro-op et la boucle sort au cycle suivant.

## 4. Lire le rapport

```bash
uv run delta0 report
```

Trois tableaux : les tirs à blanc par priorité, les 5 chemins critiques
(p95 vs budget README §7), les latences brutes par micro-op.

| Verdict | Sens | Action |
|---|---|---|
| `OK` | p95 <= budget | rien |
| `DEPASSE` | budget < p95 <= budget x 1,5 | surveiller, chercher la cause |
| `PRUDENT` | p95 > budget x 1,5 | README §11 : re-centrage anticipé à +3 % / -4,5 % |
| `INCOMPLET` | une jambe du chemin n'a aucune mesure | relancer les micro-ops manquantes |
| `AUCUN` | aucune mesure du tout | l'étape B n'a pas tourné |

P4 reste `INCOMPLET` par construction tant que `venues/swap.py` est un stub :
la jambe swap wstETH -> USDC n'existe pas encore (M2).

## 5. Limites connues à la clôture de M1-B2

- `venues/swap.py` est un stub : P4 n'est mesurable qu'en partie.
- La détection de liquidation côté Aave (`LiquidationCall` sur le Pool) est
  M2 ; seul le flanc HL est câblé.
- La porte de régime (P10) n'est pas évaluée : elle demande une moyenne 30 j
  du funding avec hystérésis 7 j, livrée en M2 avec le pipeline historique.
- Le mode prudent est **rapporté** mais pas encore appliqué au moteur de
  décision : le branchement des seuils +3 % / -4,5 % est M2.
