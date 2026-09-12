# Backlog M2

Ce que M2 doit livrer avant que du capital réel soit exposé. Le README reste la
source de vérité : si ce fichier le contredit, le README gagne.

Deux origines distinctes :

- Les **limites connues** de M1-B2, déjà recensées au RUNBOOK-M1 §6. Elles
  étaient prévues : des pièces que M1 n'avait pas à livrer.
- Les **manques révélés par la marche à blanc** (§§ 1 à 3 ci-dessous). Ceux-là
  n'étaient pas prévus. Ils viennent de sept jours d'exécution réelle et sont
  appuyés sur des mesures, pas sur une revue de code.

Les trois premiers sont classés par gravité, et tous les trois concernent la
même question : **que fait le bot quand une action critique échoue ?** Aujourd'hui,
la réponse est « rien ».

---

## 1. Reprise sur échec des actions critiques

**Ce qui manque.** Aucune logique de reprise nulle part dans le code. Une
opération qui échoue est journalisée en `failed`, et le tracer attend le cycle
suivant.

**Pourquoi c'est grave.** En M1 le cycle suivant arrive 30 minutes plus tard, et
il n'y a rien en jeu. En production, P3 (remboursement d'urgence depuis le
coussin) a un budget de **10 secondes** et P1/P2 de **2 secondes**. « Attendre le
prochain cycle » n'est pas une réponse à cette échelle.

**La preuve, mesurée.** Sur les 7 jours de marche à blanc : **5 échecs sur 2 111
opérations, soit 0,24 %**. Tous avant l'envoi de la transaction (`tx=None`), donc
sans ambiguïté ni risque de double dépense. Répartition :

```
09-05 08:33  hl_post_only_cancel   → en production : P1/P2, couper le short
09-05 16:02  aave_supply           → dépôt de re-centrage
09-05 17:02  aave_withdraw         → jambe de P3
09-05 20:33  aave_withdraw         → jambe de P3
09-06 16:05  aave_withdraw         → jambe de P3
```

Quatre des cinq portent sur des opérations qui, avec du capital, sont sur un
chemin d'urgence.

**Le point qui rend ce chiffre trompeur.** Ces 0,24 % ont été mesurés par temps
calme — marché plat, gaz à 0,02 gwei, RPC détendu. Or P3 et P4 se déclenchent
quand ETH décroche de 10 à 15 % en une heure : tout le monde transige, les
fournisseurs RPC saturent, le gaz s'envole. **La dégradation du réseau et le
stress de marché sont corrélés positivement.** Le taux d'échec en crise sera plus
élevé, exactement quand la marge est la plus mince.

**Ce que ça coûterait.** Avec 20 000 $ de capital et 2,5× d'exposition, soit
50 000 $ de collatéral : entre le déclenchement de P3 (LTV 0,79) et le seuil de
liquidation Aave, il reste quelques points de LTV — quelques pour cent de
mouvement de prix, qui se parcourent en minutes pendant un krach. Un P3 qui
échoue et n'est pas rejoué en secondes mène à la pénalité de liquidation, de
l'ordre de 5 à 7,5 % du montant liquidé, plus la destruction de la structure
delta-neutre.

**À construire.**

- Reprise immédiate avec repli progressif sur les actions des chemins P1 à P6,
  bornée par le budget du chemin : ré-essayer pendant 8 s pour un budget de 10 s
  a du sens, ré-essayer pendant 2 minutes n'en a aucun.
- Distinguer ce qui se rejoue de ce qui ne se rejoue pas. `eth_sendRawTransaction`
  est déjà exclu de la bascule RPC pour cette raison (voir `delta0/rpc.py`) : une
  transaction qui a peut-être atteint la chaîne ne se renvoie pas à l'aveugle.
  La même distinction doit exister au niveau de l'action métier.
- Escalade quand la reprise épuise son budget : passer à l'action de repli de la
  table de décision plutôt que d'abandonner en silence.

---

## 2. Invariants I1 à I8

**Ce qui manque.** Les huit invariants du README §11 ne sont **implémentés nulle
part**. Recherche dans `src/` : zéro occurrence.

**Pourquoi c'est grave.** Ils sont la seconde ligne de défense, celle qui attrape
ce que la première a laissé passer. I2 en particulier :

> *« ltv <= 0,72 en croisière ; jamais >= 0,79 plus de 5 min sans action P3
> déclenchée »*

C'est exactement l'invariant qui rattraperait le scénario du chantier 1 — un P3
qui échoue et que personne ne rejoue. Il est écrit dans la spec depuis le début
et absent du code.

**À construire.** Vérification à chaque snapshot, violation = alerte + action de
la table de décision. Les huit sont dans README §11 ; I2, I3 et I7 sont ceux qui
protègent contre une perte, les autres contre une dérive.

---

## 3. Alertes

**Ce qui manque.** `TG_TOKEN` et `TG_CHAT` sont déclarés dans `settings.py` et
dans le schéma de config, et lus par **aucune ligne de code**.

**Pourquoi c'est grave.** Une action critique qui échoue est aujourd'hui
invisible. Les 5 échecs du week-end n'ont été découverts que le dimanche soir, en
relisant le journal SQLite — deux jours après le premier. En M1 c'est sans
conséquence. Avec du capital, deux jours d'ignorance sur un P3 raté est
exactement le scénario qu'on cherche à éviter.

**Précédent à retenir.** C'est le troisième réglage fantôme trouvé pendant M1,
après `ARBITRUM_RPC_FALLBACK` (corrigé le 2026-09-03, voir `delta0/rpc.py`).
Un réglage déclaré sans consommateur donne l'illusion d'un filet qui n'existe
pas — c'est pire que pas de réglage du tout. **Passer le reste de la config au
peigne fin pour vérifier qu'il n'y en a pas d'autres.**

**À construire.** Les trois niveaux du README §12 : INFO (digest quotidien —
c'est le tableau d'exactitude), WARN (invariant mou violé, pont lent, re-centrage
exécuté), CRITICAL (P1 à P4 déclenchés, BLIND, transfert perdu).

---

## 4. Limites connues de M1-B2 (rappel du RUNBOOK-M1 §6)

Prévues, pas découvertes :

- `venues/swap.py` est un stub : la jambe swap wstETH → USDC de P4 n'existe pas.
  Le critère de sortie M1 l'exempte explicitement (RUNBOOK-M1 §5) ; M2 doit la
  livrer, ne serait-ce que pour que le backtest M2b puisse la simuler.
- Détection de liquidation côté Aave (`LiquidationCall` sur le Pool) : seul le
  flanc HL est câblé.
- Porte de régime (P10) non évaluée : demande une moyenne 30 j du funding avec
  hystérésis 7 j, donc le pipeline de données historiques.
- Mode prudent **rapporté** mais pas appliqué au moteur de décision : le
  branchement des seuils +3 % / -4,5 % reste à faire.

---

## 5. Dette technique héritée de M1

- **Base SQLite dans un dossier OneDrive.** `data/m1_run.db` a tourné 7 jours en
  mode WAL dans un dossier synchronisé, sans incident — mais les trois fichiers
  (`.db`, `-wal`, `-shm`) doivent rester mutuellement cohérents et OneDrive les
  téléverse indépendamment. Sortir la base des dossiers synchronisés avant M3.
- **Le niveau 3 du RUNBOOK ne teste pas l'executor.** `scripts/precheck_aave_fork.py`
  rejoue la séquence en parallèle du code de production et passe par
  `anvil_impersonateAccount`, donc il ne signe jamais rien. C'est pour ça que le
  bug `signed.rawTransaction` (web3 v6 → v7) a survécu aux trois premiers
  niveaux et n'est tombé qu'au premier tir réel. Faire tourner
  `AaveTraceExecutor` lui-même contre anvil, avec une clé jetable.
- ~~**Les 4 apports restés sur la branche locale `side-commit`**~~ — **fait**
  au chantier 4.9 (`0f72aa5`) : porte LIVE du README §14, fenêtre `--days`,
  artefact JSON, code de sortie non nul. La branche n'a pas été fusionnée mais
  réécrite : elle partait d'avant la correction de la définition de P1/P2 et
  aurait ramené la mauvaise. Branche supprimée le 2026-09-12.
