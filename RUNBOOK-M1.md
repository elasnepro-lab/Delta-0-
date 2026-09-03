# Runbook M1 — marche à blanc (TRACER)

Complément opérationnel du README §14. Le README reste la source de vérité :
si ce fichier le contredit, le README gagne.

Critère de sortie M1 : **7 jours de DRY_RUN, rapport p50/p95 des 5 chemins
critiques + journal des tirs à blanc.** Le rapport est produit par
`delta0 report` ; M1 est fermé quand chaque chemin y est `OK`.

Les quatre niveaux ci-dessous vont du gratuit au coûteux. Ne pas sauter un
niveau : chacun attrape une classe d'erreur que le suivant ne verrait plus.

| Niveau | Ce qui est validé | Coût | Réseau |
|---|---|---|---|
| 0 — suite de tests | la logique pure, les gardes, les seuils | 0 | non |
| 1 — observation | lectures réelles, WS, réconciliation, décision | 0 | lecture seule |
| 2 — répétition | scheduler, guard, journal, mesure de latence | 0 | lecture seule |
| 3 — fork anvil | l'ABI Aave et l'ordre exact des 6 tx | 0 | fork local |
| 4 — étape B live | les latences réelles (le livrable M1) | ~gas | écriture |

## 0. Pré-requis machine

```bash
uv sync                      # venv Python 3.12 + dépendances
cp config.yaml.example config.yaml
cp .env.example .env         # puis remplir BOT_MASTER_* (voir niveau 4)
uv run pytest -q             # doit être vert avant toute session live
uv run ruff check . && uv run mypy src tests
```

Wallet opérateur : `0x4F7ed211FcEF5555B0EC309E3bFfcCfE27750C89`.
Float attendue : ~50 USDC + ~0,005 ETH de gas sur Arbitrum.

## 1. Niveau 1 — observation seule (aucune transaction)

Mode par défaut : `config.tracer.dry_run: true`, pas de flag d'exécution.
Aucune clé privée n'est chargée.

```bash
uv run delta0 config-check
uv run delta0 status                       # recette lecture seule (M0)
uv run delta0 tracer --duration 7d --cadence 5
```

Au démarrage le tracer ouvre le WS Hyperliquid, réconcilie le journal SQLite
contre l'état on-chain (README §13), puis boucle
`snapshot -> decide -> journalise`. Il n'exécute rien.

Ce niveau ne mesure que `snapshot` et `decision`. Il ne remplit **aucun**
chemin critique : le rapport affichera `AUCUN` partout. C'est normal.

## 2. Niveau 2 — répétition à blanc des micro-ops

`--rehearse` câble les trois executors en gardant `dry_run: true`. Aucune
transaction, aucune clé privée chargée : la fabrique d'ordres Hyperliquid
lève une exception si elle est atteinte, ce qui rend un court-circuit dry-run
défaillant bruyant au lieu de silencieux.

```bash
uv run delta0 tracer --rehearse \
  --confirm aave_approve --confirm aave_supply --confirm aave_borrow \
  --confirm aave_repay --confirm aave_withdraw \
  --confirm hl_post_only_cancel --confirm bridge_out --confirm bridge_in \
  --duration 60s --cadence 5
```

C'est **exactement** la ligne de commande du niveau 4, à `--rehearse` près.
Ce qui tourne pour de vrai : lecture du mark price, WS, réconciliation,
garde-fous (liste blanche, plafond, fréquence, fichier KILL, confirmation de
première exécution), journal d'intentions SQLite, enregistrement des latences,
séquencement des 6 opérations du cycle Aave. Ce qui ne tourne pas : la
signature et l'envoi.

Chaque micro-op se déclenche dès le premier cycle, quelle que soit la machine.
Les latences mesurées sont des microsecondes (pas de réseau) et sont
enregistrées sous `dry.path.*` : elles n'entrent jamais dans le rapport des
chemins critiques, donc une répétition ne peut pas embellir un run réel.

Vérifier ensuite dans `delta0 report` que les 8 lignes `dry.path.*` sont
présentes et que les 5 chemins critiques sont toujours `AUCUN`.

`--rehearse` et `--live-micro-ops` s'excluent, et `--rehearse` est refusé si
`dry_run: false`. Une répétition ne peut pas devenir un tir réel par accident.

## 3. Niveau 3 — ABI Aave sur fork anvil

Zéro dollar en jeu, à refaire après toute modification du code Aave.

Installer Foundry (une fois par machine) :

```powershell
# PowerShell
curl -L https://foundry.paradigm.xyz | bash   # via Git Bash, puis foundryup
```

Puis :

```bash
anvil --fork-url https://arb1.arbitrum.io/rpc --port 8545 --chain-id 42161
uv run python scripts/precheck_aave_fork.py
```

Critère : 6 transactions `status=1` et solde USDC final == initial.
Découvertes contractuelles : `memory/aave_findings.md`.

## 4. Niveau 4 — étape B, micro-ops réelles

Trois verrous doivent tomber ensemble, sinon le bot refuse de démarrer :

1. `config.yaml` : `tracer.dry_run: false`
2. `.env` : `BOT_MASTER_PRIVATE_KEY` renseignée (ni vide, ni `REPLACE...`)
3. CLI : `--live-micro-ops` **et** un `--confirm <op_kind>` par opération

D'abord un seul cycle Aave, court :

```bash
uv run delta0 tracer --live-micro-ops \
  --confirm aave_approve --confirm aave_supply --confirm aave_borrow \
  --confirm aave_repay --confirm aave_withdraw \
  --duration 60s --cadence 30
```

Premier cycle Aave dans les premières secondes, ~0,05 $ de gas, le solde USDC
revient à son état initial. Vérifier ce round-trip **avant** de lancer les
7 jours.

Puis la session longue, avec HL et le pont :

```bash
uv run delta0 tracer --live-micro-ops \
  --confirm aave_approve --confirm aave_supply --confirm aave_borrow \
  --confirm aave_repay --confirm aave_withdraw \
  --confirm hl_post_only_cancel --confirm bridge_out --confirm bridge_in \
  --duration 7d --cadence 5
```

Cadences des micro-ops (`config.yaml`, section `tracer`) : Aave toutes les
30 min, HL toutes les 10 min, pont toutes les 12 h. Sur 7 jours : ~336 cycles
Aave, ~1000 aller-retours HL, ~14 traversées de pont.

En live, la réconciliation de démarrage est bloquante : un snapshot en échec
ou le moindre avertissement refuse le démarrage (code 5). On n'envoie pas de
transaction contre un état qu'on n'a pas pu vérifier.

Arrêt propre à tout moment : créer un fichier `KILL` à la racine. Le guard
refuse alors toute nouvelle micro-op et la boucle sort au cycle suivant.

## 5. Lire le rapport

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

Le p95 d'un chemin est la somme des p95 de ses jambes : une majoration
conservatrice, qui peut signaler lent un chemin qui tient son budget, jamais
l'inverse.

P4 reste `INCOMPLET` par construction tant que `venues/swap.py` est un stub :
la jambe swap wstETH -> USDC n'existe pas encore (M2).

**Exception assumée au critère de sortie.** Exiger que les 5 chemins soient
`OK` rendrait la porte M1 inatteignable : P4 porte une jambe que M1 n'a aucun
moyen de mesurer. `latency.path_meets_m1()` accepte donc un chemin `INCOMPLET`
dont le seul manque est une jambe déclarée non mesurable — et rien d'autre :

- toutes les jambes que M1 *peut* mesurer doivent avoir des échantillons
  (`missing` vide), pour qu'une micro-op oubliée ne passe jamais pour un
  manque M2 ;
- les jambes mesurées doivent tenir le budget **complet** du chemin, ce qui
  est plus sévère que de les juger sur un budget au prorata.

Le panneau vert du rapport nomme explicitement les jambes exemptées. Un
rapport qui passe en taisant ce qu'il a excusé serait pire qu'un rapport qui
échoue.

## 6. Limites connues à la clôture de M1-B2

- `venues/swap.py` est un stub : P4 n'est mesurable qu'en partie. Le
  critère de sortie l'exempte explicitement (voir §5).
- La détection de liquidation côté Aave (`LiquidationCall` sur le Pool) est
  M2 ; seul le flanc HL est câblé.
- La porte de régime (P10) n'est pas évaluée : elle demande une moyenne 30 j
  du funding avec hystérésis 7 j, livrée en M2 avec le pipeline historique.
- Le mode prudent est **rapporté** mais pas encore appliqué au moteur de
  décision : le branchement des seuils +3 % / -4,5 % est M2.
