# Déploiement — chantier 2 du plan

Le serveur n'est pas une formalité de livraison. C'est le contexte sans lequel
quatre chantiers de la phase 3 ne peuvent être ni écrits ni validés :
`loop.add_signal_handler(SIGTERM)` lève `NotImplementedError` sous Windows,
donc l'arrêt propre et l'annulation de tâche en vol sont inécrivables sur la
machine de développement.

Le README reste la source de vérité. Si ce fichier le contredit, le README
gagne.

## Ce que le code apporte déjà

`delta0 tracer --root <chemin>` fixe la racine du projet, c'est-à-dire l'endroit
où les fichiers `KILL` et `KILL_DEFLATE` sont cherchés, et où les chemins
relatifs de `--config` et `--db` sont résolus. Sans l'option, la racine est le
répertoire courant, comme avant.

Deux garde-fous viennent avec :

- une racine qui n'existe pas fait **refuser le démarrage** avec le code 7,
  avant même que la config soit lue ;
- le chemin absolu du fichier `KILL` est **écrit dans le journal et affiché à
  l'écran** au démarrage. Une racine fausse se voit au boot, pas pendant
  l'incident qu'elle devait arrêter.

C'est la réponse au point relevé dans le plan : `Path.cwd()` servait de racine,
donc sous systemd le frein d'urgence pouvait être cherché au mauvais endroit,
en silence.

## 1. La machine

Un VPS Linux quelconque suffit à cette échelle. Ce qui compte :

| Exigence | Pourquoi |
|---|---|
| Ne se met jamais en veille | La marche à blanc a tourné sous `powercfg /change standby-timeout-ac 0`, un réglage qu'on oublie de remettre |
| Horloge synchronisée | Les signatures Hyperliquid sont horodatées et une dérive les fait refuser |
| Redémarre le service au boot | Une coupure d'alimentation ne doit pas laisser le châssis sans surveillance |
| Journaux persistants | Ils sont la seule source du post-mortem exigé au README §16 |

Arborescence retenue par l'unité systemd fournie :

```
/opt/delta0          le dépôt, en lecture seule pour le service
/var/lib/delta0      la base SQLite, en lecture-écriture
```

La base sort du dépôt exprès : une base SQLite en mode WAL tient trois fichiers
qui doivent rester mutuellement cohérents, et elle n'a rien à faire dans un
dossier synchronisé ni dans un dépôt git. Pendant M1 elle a tourné sept jours
dans un dossier OneDrive, qui téléverse ces trois fichiers indépendamment.

## 2. Premier accès et durcissement SSH

Le parcours de commande OVH n'expose pas toujours de champ pour une clé
publique. Quand c'est le cas, la machine est livrée en authentification par mot
de passe et la clé se pose au premier accès.

```bash
# Depuis le poste, avec le mot de passe recu par courriel
ssh-copy-id -i ~/.ssh/id_ed25519.pub <compte>@<ip>
```

**Puis vérifier que la clé fonctionne dans une SECONDE session, en gardant la
première ouverte.** C'est l'ordre qui compte : couper les mots de passe avant
d'avoir prouvé que la clé marche est la façon classique de s'enfermer dehors,
et la seule sortie est alors la console de secours de l'hébergeur.

```bash
ssh -o PreferredAuthentications=publickey <compte>@<ip> 'echo cle ok'
```

Une fois cette preuve obtenue, et pas avant :

```bash
sudo tee /etc/ssh/sshd_config.d/durcissement.conf >/dev/null <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
sudo sshd -t && sudo systemctl reload ssh
```

`sshd -t` valide la configuration avant le rechargement : une faute de frappe
refusée maintenant vaut mieux qu'un service SSH qui ne redémarre pas.

Une IP publique reçoit des tentatives de connexion en permanence. Avec les mots
de passe coupés elles ne peuvent plus aboutir, mais elles remplissent les
journaux, ceux dont on vient de fixer la rétention :

```bash
sudo apt install -y fail2ban
sudo systemctl enable --now fail2ban
```

## 3. Installation

**Debian 13 (Trixie)**, la version stable actuelle. Rien dans ce qui suit n'est
propre à une version : `apt`, `systemd`, `journald` et `chrony` sont identiques
sur 12 et 13, et `uv` embarque sa propre version de Python, donc celle du
système ne contraint rien. À choix égal, prendre la stable la plus récente :
elle est supportée plus longtemps, et cette machine doit tourner des années.

Ni `git` ni `uv` ne sont installés par défaut. L'outillage d'abord :

```bash
sudo apt update && sudo apt install -y git curl ca-certificates
curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh
uv --version
```

`uv` va dans `/usr/local/bin` pour être visible de tous les comptes, y compris
celui du service.

Puis le compte, les répertoires et le dépôt :

```bash
sudo useradd --system --home /opt/delta0 --shell /usr/sbin/nologin delta0
sudo install -d -o delta0 -g delta0 /opt/delta0 /var/lib/delta0

sudo -u delta0 git clone -b phase0-verites-onchain \
    https://github.com/elasnepro-lab/Delta-0-.git /opt/delta0
cd /opt/delta0 && sudo -u delta0 env HOME=/opt/delta0 uv sync

# La config et les secrets
sudo -u delta0 cp config.yaml.example config.yaml   # puis ajuster
sudo -u delta0 install -m 600 /dev/null .env        # puis remplir
```

`HOME=/opt/delta0` est nécessaire parce que le compte est `--system` et que
`uv` a besoin d'un répertoire personnel inscriptible pour son cache.

`.env` reste en 600 et appartient à `delta0`. À ce stade il ne contient que
`ARBITRUM_RPC_PRIMARY` et `BOT_MASTER_ADDRESS`, qui sont les deux seuls
réglages obligatoires — l'adresse est publique. **Ne pas y mettre la clé
maître**, voir la dernière section.

Vérifier que le binaire attendu par l'unité systemd existe :

```bash
/opt/delta0/.venv/bin/delta0 version
```

## 4. L'horloge, avant tout le reste

```bash
sudo apt install chrony
sudo systemctl enable --now chronyd
chronyc tracking          # 'System time' doit rester sous 50 ms
```

À vérifier **avant** le premier ordre signé. Une dérive ne casse pas les
lectures, elle casse les écritures, et elle les casse en silence côté client :
c'est la place qui refuse.

## 5. Le service

```bash
sudo cp /opt/delta0/deploy/delta0-tracer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now delta0-tracer
```

Au démarrage, la première chose à lire est la racine :

```bash
journalctl -u delta0 -n 20 | grep -i kill
# doit afficher /opt/delta0/KILL
```

Puis vérifier que le frein fonctionne, une fois, avant d'en avoir besoin :

```bash
sudo -u delta0 touch /opt/delta0/KILL
journalctl -u delta0 -f          # le guard refuse toute nouvelle micro-op
sudo rm /opt/delta0/KILL
```

## 6. Rétention des journaux

```bash
sudo mkdir -p /etc/systemd/journald.conf.d
sudo tee /etc/systemd/journald.conf.d/delta0.conf >/dev/null <<'EOF'
[Journal]
Storage=persistent
SystemMaxUse=2G
MaxRetentionSec=90day
EOF
sudo systemctl restart systemd-journald
```

Quatre-vingt-dix jours couvrent le délai d'un post-mortem et la durée de vie
d'un agent Hyperliquid, qui expire au bout de 90 jours lui aussi.

## 7. Les alertes, avant d'allumer le service

Sans elles, une panne ne se signale nulle part. C'est ce qui a laissé la jambe
Aave morte 60 heures pendant la marche à blanc, avec 128 échecs journalisés que
personne n'a lus. Deux valeurs à obtenir, une fois.

**Le jeton du bot.** Dans Telegram, écrire à `@BotFather`, envoyer `/newbot`,
choisir un nom et un identifiant. Il répond avec un jeton de la forme
`123456789:AA...`. C'est un secret : il permet d'écrire au nom du bot.

**L'identifiant de conversation.** Les alertes partent vers un **groupe**, pas
vers une conversation privée : chaque membre les reçoit, et ajouter quelqu'un
se fait en l'invitant, sans toucher au serveur ni redémarrer le service.

1. Créer le groupe, y ajouter les personnes qui doivent être prévenues, puis le
   bot.
2. Écrire dans le groupe `/start@<identifiant_du_bot>`. En groupe, un bot ne
   voit par défaut que les commandes qui lui sont adressées : un message
   ordinaire ne lui parvient pas et `getUpdates` resterait vide.
3. Lister les conversations que le bot connaît :

```bash
curl -s "https://api.telegram.org/bot<JETON>/getUpdates" | python3 -c '
import json, sys
seen = {}
for update in json.load(sys.stdin)["result"]:
    for kind in ("message", "my_chat_member"):
        chat = update.get(kind, {}).get("chat")
        if chat:
            seen[chat["id"]] = (chat["type"], chat.get("title") or chat.get("first_name"))
for chat_id, (kind, name) in seen.items():
    print(chat_id, kind, name)
'
```

Garder la ligne de type `group` ou `supergroup`. Son identifiant est
**négatif**, et c'est normal. La commande d'origine lisait
`result[0]["message"]` et plantait en groupe : le premier événement reçu y est
souvent l'ajout du bot (`my_chat_member`), qui n'a pas de champ `message`.

Un piège : si le groupe devient un « supergroupe », par exemple quand on rend
l'historique visible aux nouveaux membres, **son identifiant change** (il
prend la forme `-100…`). L'ancien est alors refusé par Telegram avec
`group chat was upgraded to a supergroup chat`. Relancer la commande
ci-dessus et remplacer `TG_CHAT`.

Les deux vont dans `.env`, à côté des autres :

```
TG_TOKEN=123456789:AA...
TG_CHAT=-1001234567890
```

Au démarrage, le traceur annonce lequel des deux états s'applique :

```
Alertes Telegram actives (WARN et CRITICAL).
ALERTES DÉSACTIVÉES : TG_TOKEN ou TG_CHAT absent du .env.
```

Ce n'est pas décoratif. Un réglage déclaré sans consommateur donne l'illusion
d'un filet, et `TG_TOKEN` a passé tout M1 dans cet état. La ligne au démarrage
est la garantie qu'on ne se raconte plus d'histoire.

Ce qui part : les niveaux WARN et CRITICAL du README §12. Ce qui ne part pas
encore : le digest quotidien, qui *est* le tableau d'exactitude et attend les
dimensions comptables. Les répétitions se regroupent sur une fenêtre de quinze
minutes — la première alerte sort tout de suite, les suivantes reviennent en un
résumé, pour que 86 échecs identiques ne produisent pas 86 messages.

## 8. Mesurer avant de faire confiance

Quarante-huit heures d'observation en lecture seule, pour voir les heures
chargées et pas seulement dix minutes calmes, puis comparaison avec une
référence prise sur le poste **avec le même code**. Les 950 ms de p95 mesurés
pendant la marche à blanc ne servent plus de référence : ils comptaient 34
requêtes par snapshot, Multicall3 n'en envoie qu'une.

```bash
sudo -u delta0 /opt/delta0/.venv/bin/delta0 tracer \
    --root /opt/delta0 --db /var/lib/delta0/probe.db -d 2d --cadence 5
sudo -u delta0 /opt/delta0/.venv/bin/delta0 report --db /var/lib/delta0/probe.db
```

Plus lent que chez soi veut dire que la machine ou son fournisseur RPC est le
mauvais choix, et il vaut mieux le savoir avant d'y installer la boucle. La
distance, elle, ne se voit pas : l'aller-retour réseau vaut 22 ms vers
Hyperliquid contre 237 ms de traitement chez eux. Le levier de vitesse est la
cadence et le regroupement des lectures RPC, pas la géographie.

## La clé maître n'a rien à faire ici pour l'instant

Le traceur en DRY_RUN ne signe rien : il lui suffit de l'URL du RPC et de
l'adresse maître, qui est publique. C'est tout ce dont la phase 3 a besoin pour
être écrite et validée, puisque ce qu'on vient chercher sur cette machine est
un Linux où `add_signal_handler` existe.

La clé arrive avec le chantier 5.3, quand il y aura deux signataires : un agent
Hyperliquid sur le serveur, qui peut passer des ordres mais ne peut ni
transférer ni retirer, et la clé maître ailleurs pour le pont et l'écrémage.
Cette frontière est une contrainte de la place, mesurée en phase 0, pas une
bonne pratique optionnelle. Provisionner maintenant et signer plus tard n'est
donc pas un compromis, c'est l'ordre juste.

## Ce qui n'est pas fait ici

- **Le chien de garde externe** (chantier 5.2). Un processus figé n'écrit rien,
  donc `Restart=always` ne le relèvera pas et aucune alerte ne partira. Il faut
  un observateur hors de la machine.
- **La rotation de l'agent Hyperliquid** (chantier 5.3). L'agent expire au bout
  de 90 jours et les executors cesseraient de signer sans rien dire.
- **`.env` reste lu relativement au répertoire courant** (`env_file=".env"`
  dans `settings.py`). L'unité fixe donc `WorkingDirectory`. Cette dépendance
  est moins dangereuse que celle du fichier `KILL` parce qu'elle échoue fort :
  sans clé, le bot refuse de démarrer avec le code 3. À rendre explicite au
  passage du chantier 6.6.
