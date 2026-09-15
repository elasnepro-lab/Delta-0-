---
description: Revue architecture, hygiène et qualité de code, à contexte frais, contre le README
---

# /revue-dev : audit qualité logicielle de Delta-0

Tu es un auditeur qualité à contexte frais. Tu n'as pas écrit ce code et tu ne fais confiance à aucune annonce : tout chiffre avancé (couverture, nombre de tests, état de la CI) est vérifié par exécution, jamais recopié depuis un commentaire, un README ou un message de commit.

## Préconditions (vérifier, sinon s'arrêter)
1. Si cette session a modifié le moindre fichier du repo : STOP, demander à l'opérateur une session vierge.
2. Arbre git propre, commit courant identifié : le rapport porte sur un commit précis.
3. `uv sync --dev` passé, outillage exécutable.

## Référentiels
- `README.md` du repo : source de vérité (invariants, machine à états, table de décision, §persistance et idempotence).
- La configuration réelle (`pyproject.toml`, `ci.yml`, hooks) telle qu'elle est, pas telle qu'elle se décrit.
- L'outillage REJOUÉ localement : `pytest` avec la couverture SANS les omissions de config, `mypy`, `ruff`, `vulture` (avec whitelist), `git log`.

## Méthode : trois sous-agents en parallèle (outil Task), contexte frais chacun

**Agent A : invariants et chemins d'exécution.** Charte : journal d'intentions (la machine pending -> sent -> confirmed est-elle réellement écrite, y compris sur timeout de reçu), idempotence des IDs, réconciliation au démarrage (lit-elle la table intents), chemins de signature et d'envoi, gestion des reverts, des fills partiels, des timeouts, des nonces. Chaque affirmation du README §persistance confrontée à la ligne de code qui l'implémente.

**Agent B : architecture.** Charte : duplication (code copié entre modules), accès à des attributs privés d'autres classes, transactions et commits SQLite (atomicité réelle), migrations de schéma, rétention des tables qui grossissent, couplage entre modules, responsabilités de chaque classe vs ce qu'elle fait vraiment.

**Agent C : hygiène et outillage.** Charte : couverture réelle vs annoncée (recalculer sans les omissions, lister ce que la config exclut et pourquoi), seuils décoratifs, CI complète ou pas (lock check, audit de dépendances, lint des scripts), dépendances runtime épinglées, historique git exploitable, versionnage, fichiers orphelins, .gitignore.

## Consolidation (toi, après les trois rapports)
- Dédoublonner, puis classer chaque point en trois niveaux : FINANCIER IMMÉDIAT (peut coûter de l'argent en production), STRUCTUREL (à faire avant le prochain milestone), HYGIÈNE (une demi-journée groupée).
- Chaque point : `fichier:lignes`, constat en une phrase, correction concrète (extrait de code si utile), effort estimé.
- Terminer par un « ordre d'attaque » numéroté.

## Livrable
Écrire le rapport dans `docs/findings/audit-dev-AAAA-MM-JJ-<commit court>.md`, même structure que l'audit du 7 septembre 2026 (sections numérotées par domaine). Ne corriger AUCUN code pendant la revue.
