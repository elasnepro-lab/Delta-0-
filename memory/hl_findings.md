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

## 6. markPx et oraclePx sont deja exposes (chantier 0.2, A6)

`info.meta_and_asset_ctxs()` renvoie, par actif, tout ce dont les seuils HL ont
besoin. Le bot ne s'abonne aujourd'hui qu'a `allMids`, donc il pilote ses seuils
sur le mid du carnet alors que les liquidations utilisent le mark.

```
funding, openInterest, prevDayPx, dayNtlVlm, premium,
oraclePx, markPx, midPx, impactPxs, dayBaseVlm
```

Mesure du 2026-09-08 en regime calme, ETH, 15 echantillons sur 45 s :

```
|mark - mid|    mediane 0,60 bps   max 1,41 bps
|mark - oracle| mediane 3,71 bps   max 3,95 bps
```

En regime calme l'ecart est negligeable — l'audit porte sur le stress, qui ne se
mesure pas a la demande. Ce qui est acquis : **le passage au mark ne coute
rien**, les champs sont deja dans la reponse. `impactPxs` est un bonus utile
pour estimer le slippage d'un IOC avant de l'envoyer.

## 7. Un compte inexistant est refuse au PREMIER niveau (chantier 0.2, C1)

Ordre signe par une cle jetable jamais financee, sur le testnet :

```json
{"status": "err",
 "response": "User or API Wallet 0x1ef3... does not exist."}
```

`ensure_ok` attrape donc bien ce cas. **Le refus niche dans une enveloppe
`status: ok` suppose un compte qui existe** — marge insuffisante, notionnel sous
le minimum, prix trop loin de l'oracle.

### C1 CONFIRME sur un compte existant (testnet, 2026-09-08)

Deux ordres refuses, signes par l'agent sur le compte maitre testnet :

```json
{"status": "ok",
 "response": {"type": "order", "data": {"statuses": [
   {"error": "Order must have minimum value of $10. asset=4"}]}}}

{"status": "ok",
 "response": {"type": "order", "data": {"statuses": [
   {"error": "Insufficient margin to place order. asset=4"}]}}}
```

`ensure_ok` **laisse passer les deux**. En M2, un IOC P2 refuse pour marge
insuffisante serait donc marque `confirmed`, avec un echantillon
`path.p1_p2_hl_order` enregistre pour un ordre qui n'a jamais existe — un
chiffre du rapport M1 representant du vide.

Correction : `ensure_ok` doit inspecter `response.data.statuses` et lever des
qu'une entree porte une cle `error`. Les deux enveloppes ci-dessus sont a
reprendre telles quelles comme fixtures de test (point D2 de l'audit).

Note de forme : `asset=4` est l'index de ETH dans l'univers perp, pas un code
d'erreur. Le message est en anglais et non structure — il faut donc detecter la
presence de la cle `error`, jamais filtrer sur le texte.

## 8. A7 n'est pas bloquant : la solution ne depend pas du diagnostic

La question « `marginUsed` inclut-il le PnL latent ? » demande une position
ouverte pour etre tranchee. Mais la correction proposee — **ne plus reconstruire
un ratio et declencher sur la distance a `liquidationPx`**, que la place fournit
— est valable dans les deux cas. Le diagnostic peut donc attendre le compte
testnet sans retarder l'ecriture du code.

Reste vraiment bloquant par le faucet : C1 (forme du refus), A5 (une fermeture
partielle deplace-t-elle `liquidationPx` ?) et A4 (regle des 20 % sur les
sorties). A5 est le seul qui change la conception de P2.

## 9. Un agent ne peut pas transferer de fonds (chantier 0.2, K7)

Agent `delta0-phase0` autorise sur le compte maitre testnet, puis
`usd_class_transfer(500, to_perp=True)` signe par l'agent :

```json
{"status": "err",
 "response": "Must deposit before performing actions. User: 0x6765...3799"}
```

L'erreur nomme **l'adresse de l'agent**, pas celle du maitre. Les transferts
sont des actions signees par le compte lui-meme (EIP-712 lie au signataire),
pas des actions L1 delegables. Un agent place et annule des ordres, ajuste le
levier et la marge isolee ; il ne transfere ni ne retire.

C'est exactement la separation que K7 attendait, et elle est verifiee. Elle a
une consequence de conception : **la pompe P5/P6 et l'ecremage P9 ne peuvent pas
etre signes par l'agent**. Il faut soit la cle maitre pour ces chemins, soit un
signataire separe (`delta0-signer`), ce qui renforce le decoupage propose en E3.

Duree de validite constatee : agent approuve le 2026-09-08, `validUntil` au
**2026-12-07**, soit environ 90 jours. Le calendrier de rotation n'est donc pas
optionnel : sans renouvellement, les executors M2 cesseraient de signer au bout
d'un trimestre, en silence.

Lecture des agents actifs, sans authentification :

```
POST /info {"type": "extraAgents", "user": "<adresse maitre>"}
-> [{"name":"delta0-phase0","address":"0x6765...","validUntil":1796679086309}]
```

A cabler comme controle au boot en M2 : refuser de demarrer si l'agent expire
dans moins de 7 jours.

## 10. A5 CONFIRME : une fermeture partielle ne deplace pas le prix de liquidation

Position isolee ETH 10x sur le testnet, 2026-09-08, ouverte puis reduite de 30 %
par l'agent :

```
AVANT  liquidationPx 2 673,95   taille 0,050   marge 12,3467
APRES  liquidationPx 2 673,63   taille 0,035   marge  8,6275
                                              deplacement -0,012 %
```

La place libere la marge **proportionnellement** a la taille fermee
(12,3467 x 0,7 = 8,64). Le rapport marge/notionnel est donc inchange, et le prix
de liquidation avec lui.

**Consequence : P2 tel que specifie ne protege de rien.** Le README suppose que
fermer 30 % du short eloigne la liquidation ; c'est faux. Et comme `decide()`
recalcule a chaque cycle de 5 s sur un snapshot inchange, P2 se rappellerait :
30 %, puis 30 % du reste, jusqu'a un short a zero en une minute — le bot
fabriquant lui-meme la jambe nue qu'il est cense empecher. Le verrou de refeu
(chantier 2.1) borne deja la casse, mais ne rend pas P2 utile pour autant.

## 11. Le remede tient : ajouter de la marge isolee deplace bien la liquidation

Meme position, `update_isolated_margin(+5 USDC)` :

```
marge          8,6310 -> 13,6065
liquidationPx  2 673,63 -> 2 813,68     +5,238 %
reponse        {"status": "ok", "response": {"type": "default"}}
```

+5 USDC sur 86,8 de notionnel (5,8 %) eloignent la liquidation de 5,2 %. C'est
**local, une seule requete, signable par l'agent** — exactement ce que le
principe cardinal demande d'un chemin de survie.

Conception a retenir pour M2 : P2 devient « ajouter de la marge isolee depuis la
reserve HL libre », et la fermeture partielle redevient le dernier recours quand
la reserve est vide — auquel cas sa taille doit etre calculee pour ramener la
position sous controle **en une seule fois**, puisqu'un second tir n'apporterait
rien. Cela impose la reserve USDC libre sur HL (point A16 de l'audit), a chiffrer
dans le classeur.

## 12. A7 : non tranche, et devenu sans objet

Le discriminant demande un PnL latent significatif ; celui de la mesure valait
-0,007 USDC, soit un ordre de grandeur sous les residus :

```
positionValue 86,8175   levier 10   pv/levier 8,6817
marginUsed    8,6310    unrealizedPnl -0,0070   pv/lev+pnl 8,6747
```

Aucune des deux formules ne colle a 0,04-0,05 pres. Conclure serait du bruit.

Mais la question perd son enjeu : **`liquidationPx` est fourni directement par
la place** (verifie ci-dessus, et il reagit correctement a la marge). La
correction prevue — declencher sur la distance a `liquidationPx` au lieu de
reconstruire un ratio — ne depend pas de la semantique de `marginUsed`. A7 sort
donc du chemin critique sans avoir ete tranche, et c'est acceptable.

## 13. A4 : pas de regle des 20 % sur le retrait de marge isolee

Position isolee ETH 10x, marge portee a 18 % puis retiree par paliers de 2 USDC
(testnet, 2026-09-09, signe par l'agent) :

```
ratio 18,00 %  retrait -2  OK
ratio 16,39 %  retrait -2  OK
ratio 14,78 %  retrait -2  OK
ratio 13,18 %  retrait -2  OK
ratio 11,57 %  retrait -2  REFUSE
   {"status":"err","response":"Position does not have sufficient margin for reduction."}
```

Le pas refuse aurait amene le ratio a 9,955 %, soit juste sous le 1/10 impose par
le levier 10x. **Le plancher est le reglage de levier, pas un seuil de 20 %.**

Consequence : la marge isolee excedentaire est librement recuperable jusqu'au
levier choisi, et cette action est **signable par l'agent**. C'est la premiere
etape de la pompe descendante P6, et elle n'est pas contrainte.

Ce qui reste non teste : le **retrait vers Arbitrum** (`withdraw_from_bridge`),
qui demande une signature maitre. La regle des 20 %, si elle existe, porterait
sur ce chemin-la. A garder ouvert, mais l'enjeu baisse : P6 libere d'abord de la
marge (non contraint), et ne retire ensuite que du solde libre.

Sur compte unifie, le transfert perp->spot n'existe pas : cette troisieme
variante de A4 ne s'applique pas a notre modele.

## 14. Piege : `withdrawable` vaut 0 des qu'une position est ouverte

Mesure ci-dessus, a chaque palier, y compris a 18 % de ratio de marge :

```
accountValue 22,36   withdrawable 0,0000
```

Sur un compte unifie, `accountValue` ne rapporte que l'equite des positions
ouvertes, et `withdrawable` ne compte que ce qui pourrait quitter cette poche —
soit rien tant que tout est engage en marge isolee. Les fonds libres, eux, sont
sur le spot (998 USDC au moment de la mesure) et n'apparaissent dans aucun des
deux champs.

C'est le meme piege que `accountValue = 0` decrit au point 1, et il est plus
dangereux : **du code qui lirait `withdrawable` pour dimensionner une pompe
descendante conclurait toujours qu'il n'y a rien a pomper.** Le montant
mobilisable est la somme de la marge isolee excedentaire (point 13) et du solde
spot libre, jamais `withdrawable`.

## 15. Retrait vers Arbitrum : un agent ne peut pas, et le testnet ne permet pas de conclure

Complement du point 13, teste avec la cle maitre sur le testnet le 2026-09-09.

**Acquis solide.** Un agent ne peut pas davantage retirer que transferer :

```
withdraw_from_bridge signe par l'agent
-> {"status":"err","response":"Must deposit before performing actions. User: 0x6765...3799"}
```

La frontiere est donc complete : **un agent trade, il ne deplace aucun fonds.**
Ni `usd_class_transfer`, ni `withdraw_from_bridge`.

**Acquis solide.** Un retrait signe par la cle maitre fonctionne, et la grille de
frais du point 5 est confirmee par le journal :

```
userNonFundingLedgerUpdates -> {"type":"withdraw","usdc":"4.0","fee":"1.0"}
```

5 USDC demandes, 1 de frais, 4 credites.

**NON CONCLUANT.** La question de fond — une position ouverte empeche-t-elle le
retrait ? — n'a pas de reponse exploitable ici. Le deroule :

```
sans position, retrait de 5      ACCEPTE
position isolee 3 ETH, 984       refuse
   puis 800, 600, 400, 300, 260, 245, 230, 200, 50, 10   tous refuses
position croisee 3 ETH, 200/50/10                        tous refuses
position reduite a 1,5 puis 0,2, retrait de 10           refuses
position entierement fermee, retrait de 10               refuse
sans position, retrait de 10 toutes les minutes pendant 4 min  refuses
```

Le refus persiste **apres fermeture complete et apres attente**, donc il ne
s'explique ni par la position ni par une simple limitation de frequence. Le
journal ne montre qu'un seul retrait sur l'heure. Le testnet parait plafonner
les retraits d'une maniere qui rend la question intestable ici.

Message toujours identique et inexploitable : `"Error withdrawing from bridge"`.
A noter pour le code : c'est un refus de **premier niveau**, donc `ensure_ok`
l'attrape — contrairement au refus d'ordre du point 7.

**Ce qu'il faut en retenir pour la conception, malgre l'absence de reponse :**
P6 ne doit pas etre ecrit en supposant que le retrait aboutit. La sequence sure
est « liberer la marge isolee » (non contraint, point 13, et signable par
l'agent) puis « retirer », cette seconde etape devant traiter le refus comme une
issue normale et non comme une anomalie. Le budget de temps de P6 doit couvrir
un echec et une reprise.

**Reste a trancher, sur le mainnet et avec un petit montant** : un retrait de
3 USDC avec une position ouverte. C'est le seul environnement ou la reponse
vaudra quelque chose. Cout : 1 USDC de frais.

## 16. A4 TRANCHE sur le mainnet : une position ouverte n'empeche pas le retrait

Mesure du 2026-09-09 sur le compte reel, position minuscule ouverte puis
refermee, retrait signe par la cle maitre :

```
depart             spot 29,80   accountValue 0,00   withdrawable 0,00   positions 0
position ouverte   spot 29,79   accountValue 1,24   withdrawable 0,00   positions 1
   0,005 ETH isole 10x, rempli a 2 488,7 (~12,44 USD de notionnel)
retrait de 3 USDC  -> {"status":"ok","response":{"type":"default"}}   ACCEPTE
final              spot 26,79   accountValue 0,00   withdrawable 0,00   positions 0
```

**Il n'y a pas de regle des 20 % qui bloquerait la pompe descendante.** P6 peut
retirer avec le hedge en place. Le point A4 de l'audit est infirme.

Deux enseignements au-dela de la reponse :

**1. Le retrait puise dans le spot, pas dans la poche perp.** Le compte affichait
`accountValue` a 1,24 et a laisse sortir 3 USDC — plus que toute l'equite perp.
Sur un compte unifie, la marge isolee d'une position et le solde retirable sont
deux choses distinctes, et la position ne fait pas barrage.

**2. `withdrawable` a 0,00 pendant qu'un retrait de 3 USDC aboutit.** C'est le
contre-exemple qui cloue le piege du point 14 : ce champ ne mesure pas ce qui
peut quitter le compte. Du code qui s'en servirait pour dimensionner une pompe
conclurait qu'il n'y a rien a pomper, alors que le retrait passe. Le montant
mobilisable se lit sur le solde spot.

**Sur le testnet, les memes tentatives echouaient toutes** (point 15), y compris
sans position et apres attente. C'etait donc un artefact de la plateforme de
test, pas une regle de la place. Enseignement de methode : ne pas conclure une
regle metier depuis un refus de testnet.

Reserve honnete : la position testee etait minuscule (12 USD de notionnel, 1,24
de marge, pour 29,80 de solde). Une contrainte pourrait apparaitre quand la
position est grande devant le compte. Ce qui est acquis, c'est qu'il n'existe pas
de barrage systematique.
