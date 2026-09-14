## Revues à contexte frais (protocole permanent)

Le projet a deux revues obligatoires, définies dans `.claude/commands/` :

- `/revue-dev` : architecture, hygiène, qualité de code. Cadence : à la fin de chaque milestone, avant de le déclarer livré.
- `/revue-finance` : exactitude financière du montage (le code calcule-t-il la bonne chose). Cadence : au gel de la spec M2, puis avant tout passage LIVE_SMALL, puis avant LIVE.

Règles non négociables :
1. Une revue s'exécute TOUJOURS dans une session neuve qui n'a pas écrit ni modifié le code audité. Si la session courante a touché au code, refuser et demander une session vierge.
2. Les deux revues ne partagent jamais la même session.
3. En cas de chevauchement entre trouvailles, la lecture financière prime sur la lecture stylistique.
4. Les désaccords entre agents ne se votent pas : ils remontent à l'opérateur avec les deux arguments côte à côte.
5. Aucune correction de code pendant une revue. La revue produit un rapport ; les correctifs sont un travail séparé, planifié depuis le rapport.
6. Les rapports atterrissent dans `docs/findings/` sous la forme `audit-dev-AAAA-MM-JJ-<commit>.md` et `audit-finance-AAAA-MM-JJ-<commit>.md`. Un milestone n'est pas livré tant que son rapport de revue n'existe pas et que ses points bloquants ne sont pas traités.
7. Toute valeur de plateforme (paramètres de risque Aave, maintenance margin HL, décimales, frais, minimums) citée dans un rapport est lue au moment de l'audit, jamais de mémoire. Une valeur non vérifiable est marquée NON VÉRIFIÉE.
