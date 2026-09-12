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

La base sort du dépôt exprès. `BACKLOG-M2.md` §5 le demande : une base SQLite
en mode WAL tient trois fichiers qui doivent rester mutuellement cohérents, et
elle n'a rien à faire dans un dossier synchronisé ni dans un dépôt git.

## 2. Installation

```bash
sudo useradd --system --home /opt/delta0 --shell /usr/sbin/nologin delta0
sudo install -d -o delta0 -g delta0 /opt/delta0 /var/lib/delta0

# Le dépôt, puis les dépendances
sudo -u delta0 git clone <url> /opt/delta0
cd /opt/delta0 && sudo -u delta0 uv sync

# La config et les secrets
sudo -u delta0 cp config.yaml.example config.yaml   # puis ajuster
sudo -u delta0 install -m 600 /dev/null .env        # puis remplir
```

`.env` reste en 600 et appartient à `delta0` : il porte la clé maître.

## 3. L'horloge, avant tout le reste

```bash
sudo apt install chrony
sudo systemctl enable --now chronyd
chronyc tracking          # 'System time' doit rester sous 50 ms
```

À vérifier **avant** le premier ordre signé. Une dérive ne casse pas les
lectures, elle casse les écritures, et elle les casse en silence côté client :
c'est la place qui refuse.

## 4. Le service

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

## 5. Rétention des journaux

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
