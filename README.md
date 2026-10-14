# Bot delta neutral PaperTrade ($PAPER)

Bot qui farme du $PAPER sur [PaperTrade](https://papertrade.xyz) avec deux wallets, en reprenant la
méthode décrite par [@rusk0f](https://x.com/rusk0f/status/2109921167830389061) :

- **delta neutral sur 2 comptes** : le wallet A ouvre un long, le wallet B un short, même coin, même marge, levier x250,
  marge calculée pour une position de 11 000 $ (44 $ par jambe) ;
- **jamais de liquidation** : dès que le prix a bougé de `CLOSE_MOVE_PCT` (0,70 %), ramené à 0,15 % avant la
  liquidation quand celle-ci est plus proche (en x250 : liquidation à 0,35 %, fermeture à 0,20 %), le bot ferme
  **les deux jambes**. La jambe perdante réalise sa perte et
  mint du PAPER, la jambe gagnante récupère le mouvement moins le haircut de PaperTrade ;
- **staking automatique** : dès qu'un wallet détient 1 000 PAPER non stakés, le bot les stake, sans attendre de « go » ;
- **rééquilibrage des USDC** : après chaque cycle, un wallet a gagné et l'autre perdu. Le bot retire la moitié de
  l'écart du wallet riche vers HyperCore, puis l'envoie à l'adresse de dépôt PaperTrade de l'autre wallet ;
- **arrêt automatique** : dès qu'un cycle a coûté plus de 7 $ pour 1 000 PAPER, le bot arrête d'ouvrir des paires
  (l'émission baisse avec le temps), mais continue de staker. Le coût cumulé de tous les cycles (avortés compris)
  est aussi surveillé.

```
idle ──► (rééquilibrage A⇄B si besoin) ──► ouverture long A + short B ──► surveillance du prix BBO Hyperliquid
  ▲                                                                              │ |mouvement| ≥ 0,20 %
  └── stake du PAPER ◄── bilan du cycle (coût $, PAPER mintés) ◄── fermeture des 2 jambes
```

## ⚠️ À lire avant de mettre de l'argent

- **API non officielle.** PaperTrade ne publie ni SDK ni ABI. Les endpoints, les types EIP-712 et les adresses
  viennent de la rétro-ingénierie du site par la communauté (10/10/2026). Ils peuvent changer sans prévenir.
  Le bot vérifie ce qu'il peut (domaines EIP-712 on-chain, minimums via `/state/trading`), mais **la lecture des
  positions ouvertes (`/query/accounts/{adresse}/trades`) n'a jamais pu être testée en conditions réelles** :
  suivez la mise en route ci-dessous, `run` refuse de démarrer sans les mini-positions de test.
- **Conditions d'utilisation.** En phase 1, PaperTrade réserve le trading au site et à des relayers whitelistés,
  et contourner le site « expose à une exclusion ». Le bot envoie les mêmes intents signés que le site, au même
  relayer, en s'identifiant honnêtement, mais c'est de l'automatisation : c'est votre risque.
- **Capital et notionnel minimum.** Une position doit faire au moins 10 000 $ de notionnel. Par défaut le bot vise
  11 000 $ en x250, soit 44 $ de marge par jambe ; quand les soldes baissent il ouvre plus petit, jusqu'au minimum
  (40 $ en x250). Avec 100 $ par wallet, ~120 $ sur 200 $ peuvent donc être « brûlés » pour minter, ~80 $ restent.
  En simulation (haircut optimiste, émission 50 PAPER/$) : 29 cycles, ~32 000 PAPER, ~122 $ dépensés (3,80 $/1 000).
  Mais en x250 la fermeture se fait à 0,20 % (liquidation à 0,35 %) au lieu de 0,70 % en x100 : le haircut
  sur de petits mouvements pèse plus, et le coût par PAPER est ~1,5 à 2 fois celui de x100 selon l'hypothèse.
  Si la file d'attente des profits de PaperTrade est active, le gain de la jambe gagnante n'est pas disponible
  tout de suite et les cycles s'arrêtent plus tôt.
- **Le coût par PAPER a augmenté.** rusk0f s'est arrêté quand un cycle dépassait 7 $/1 000 PAPER. Si l'émission
  actuelle est déjà basse, le plafond se déclenchera vite. `python -m ptbot plan` l'estime avant de lancer.
- **Risques du protocole.** Contrats non vérifiés et upgradables, LP unique, profits mis en file d'attente si la
  LP manque de liquidités, PAPER non transférable pour l'instant. Un gap de prix au-delà de 0,35 % (x250) entre deux
  lectures liquide la jambe perdante (le bot le gère, mais le cycle coûte plus cher).
- **Clés privées.** Le bot a besoin des clés des deux wallets : le staking et les retraits sont signés par le
  wallet principal. Utilisez deux wallets dédiés, ne mettez que ce que vous acceptez de perdre, gardez `.env` et
  `data/session_keys.json` pour vous.
- **Wallets dédiés.** Le bot **ferme toute position qu'il ne gère pas** sur A et B (un ordre exécuté en retard,
  une réponse perdue… ou un trade fait à la main). Ne tradez pas à la main sur ces wallets.

## Installation

Python 3.10+.

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows : .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # puis remplissez .env
```

Chaque wallet doit déjà avoir un compte PaperTrade avec des USDC déposés (via le site). Les deux wallets doivent
aussi exister sur Hyperliquid (HyperCore), puisque le rééquilibrage passe par là.

## Mise en route (dans cet ordre)

Le bot n'a jamais pu parler au vrai PaperTrade pendant son développement : l'API n'est pas documentée et le format
exact de plusieurs réponses est deviné. Cette procédure vérifie chaque hypothèse **avec au plus 10 $ en jeu par
wallet** avant de lui confier 100 $. `python -m ptbot run` refuse de démarrer tant que les étapes 6 et 8 n'ont pas
réussi (session keys, mini-positions de test) et tant que l'horloge, les adresses de dépôt ou `balances()` ne sont
pas conformes.

0. **Décidez du risque de la phase 1.** Pour l'instant PaperTrade réserve le trading au site et à des relayers
   autorisés ; contourner le site « expose à une exclusion ». Le bot envoie les mêmes ordres signés que le site,
   mais c'est de l'automatisation. Si vous ne voulez pas risquer A et B, arrêtez-vous là et tradez à la main en
   suivant `python -m ptbot plan`.
1. **Machine** : horloge synchronisée (NTP : `timedatectl set-ntp true`), qui ne se met pas en veille. Deux wallets
   dédiés, chacun avec ses USDC sur PaperTrade et un compte HyperCore existant. Une alerte que vous verrez vraiment :
   `tail -F data/bot.log | grep --line-buffered -E 'PAUSE|ERROR|refus'` vers une notification. Une pause avec une
   paire ouverte demande d'agir en quelques minutes.
2. **Contrôle en lecture seule** (sans clé, rien n'est dépensé) :
   `python tools/preflight_live.py --wallet-a <A> --wallet-b <B> --deposit-a <DEPOSIT_A> --deposit-b <DEPOSIT_B>`.
   Tout `FAIL` est bloquant. Il faut notamment : décalage d'horloge < 5 s, `eip712Domain()` lisible (notez le nom
   du domaine), minimums `(10.0, 10000.0)`, sur les comptes publics `ids N/N` sans `unknown`, `balances() return
   size 32 bytes`, adresses de dépôt API = `.env`. Si `profit queue` est actif, `userQueuedTotal` doit être lisible.
3. **`python -m ptbot probe`**, puis ouvrez `data/probe/probe.json` : domaines lus (pas `null`), soldes égaux à
   ceux du site, `usdc_queued` est un nombre, `trades_order`, `open_positions_parsed` vide si vous n'avez rien
   d'ouvert.
4. **Position manuelle sur le site** (bot arrêté, avant d'enregistrer la clé du bot) : sur A, la plus petite
   position possible (10 $ x1000). Pendant qu'elle est ouverte, relancez `probe` : `open_positions_parsed` doit la
   contenir (bon id, BTC, sens, prix d'entrée) et `usdc_papertrade` doit avoir baissé d'environ 10 $. Fermez-la (ou
   laissez-la liquider) et relancez `probe` : elle doit avoir disparu. **Sinon, n'allez pas plus loin** et
   envoyez-moi `trades_raw_first_page` pour adapter `ptbot/positions.py`.
5. **Aller-retour manuel de 5 à 10 $** : retirez de A vers HyperCore sur le site (notez s'il faut une deuxième
   étape, les frais, spot ou perp, la durée), puis envoyez-les vers l'adresse de dépôt de B. Si le crédit prend plus
   d'une heure, le bot se mettra en pause pendant ses rééquilibrages.
6. **`python -m ptbot register`** (bot arrêté) puis attendez 5 minutes. Refus « signature » : le format a changé,
   n'allez pas plus loin. Refus « origin/cookie » : le relayer refuse les clients hors site, arrêtez-vous.
7. **Ne réactivez pas le trading sur le site** pour A ou B ensuite : si le site enregistre sa propre clé, celle du
   bot peut être révoquée (le bot en réenregistre une, mais une fermeture peut échouer entre-temps). Si vous l'avez
   fait, relancez `register`.
8. **Mini-position signée par le bot** (perte maximale 10 $ par wallet) :
   `python tools/preflight_tiny_trade.py --wallet A` (à blanc), puis
   `python tools/preflight_tiny_trade.py --wallet A --i-understand-this-trades`, et pareil pour B. Toutes les
   lignes doivent être `PASS` ; notez les latences. Si l'ouverture met plus de 60 s, montez `OPEN_DEADLINE_S`. En
   x250, 0,15 % seulement séparent la fermeture de la liquidation : si la fermeture met plus de 10 s, baissez le
   levier (`LEVERAGE`/`MAX_LEVERAGE`) avant d'aller plus loin.
9. **`python -m ptbot plan`** : marge 44 $, x250, sortie ±0,20 % contre liquidation ±0,35 %, émission, coût estimé.
10. **Premier lancement supervisé** : `python -m ptbot run` et
    `tail -f data/bot.log`. Surveillez avec `python -m ptbot status` (pas avec le site). Vérifiez la ligne
    « Paire ouverte (A …, B …) », l'absence de « refusé », le temps entre « fermeture des deux jambes » et la
    disparition des positions, puis le premier rééquilibrage de bout en bout.
11. **Laissez-le tourner seul après au moins 3 cycles propres** (coûts et PAPER cohérents avec le site).
    Refaites l'étape 2 après toute mise à jour de PaperTrade (contrats upgradables).

**Procédure d'urgence** : créez le fichier `STOP`, attendez l'arrêt du bot, fermez les deux jambes (`close-all`,
ou sur le site, puis `register` avant de relancer). `close-all` refuse de tourner pendant que le bot tourne.

## Commandes

| Commande | Effet |
|---|---|
| `probe` | lecture seule : diagnostic complet de l'API et des soldes |
| `plan` | lecture seule : décision du bot à l'instant T |
| `sim` | simulation hors-ligne (`--hours`, `--usdc`, `--emission`, `--vol`, `--live-prices`) |
| `register` | (ré)enregistre les session keys |
| `run` | lance le bot |
| `status` | soldes, positions, PAPER générés, coût moyen |
| `stake-now` | stake immédiatement tout le PAPER non staké |
| `close-all` | ferme toutes les positions des deux wallets |
| `resume` | lève une pause (`--reset-costs` oublie l'historique de coût) |

**Arrêter le bot** : `Ctrl+C` (ou `kill`, ou un fichier `STOP` dans le dossier). Le bot termine l'étape en
cours puis s'arrête ; un second `Ctrl+C` force l'arrêt. Une paire ouverte **reste ouverte** : au redémarrage, le
bot reprend le cycle en cours (`data/state.json`). Pour tout couper : `close-all`.

**Ce qui est géré en cas de problème** :
- réponse perdue, erreur réseau ou erreur 5xx sur un ordre : l'ordre est considéré comme « peut-être exécuté ».
  Le bot le surveille jusqu'à son expiration (2 min), ferme et comptabilise une exécution tardive avec son cycle,
  et n'ouvre jamais une nouvelle paire avant ;
- une jambe ouverte seule : sa perte est surveillée et elle est fermée dès qu'elle atteint la distance de sortie,
  ou à l'expiration de l'autre ordre ;
- l'état est sauvegardé **avant** chaque ordre et chaque transfert : un crash ou un redémarrage reprend où il en
  était, sans rouvrir de paire ni retirer deux fois ;
- une lecture d'API vide ou en retard ne ferme pas la paire et ne cache pas une position à fermer (3 lectures
  manquées d'affilée sont nécessaires) ; si le prix d'entrée est illisible, le bot retient le pire prix vu ;
- si l'API des positions tombe pendant qu'une paire est ouverte, le bot continue de surveiller le prix toutes les
  2 s et renvoie les fermetures à l'aveugle (une fermeture en double est sans effet) ;
- chaque jambe sort sur son propre prix d'entrée, même si les deux ont été exécutées à des prix différents ;
- les transferts lents (retrait vers HyperCore, crédit du dépôt) sont attendus jusqu'à 2 h, avec relance automatique
  si un retrait a expiré sans être exécuté ou si un envoi n'est jamais parti.

`python -m ptbot resume` fonctionne aussi pendant que le bot tourne (il le prend en compte au tour suivant). Un
`data/state.json` d'une version précédente du bot, laissé en plein cycle, est refusé au démarrage avec la marche à
suivre.

**Pauses** : le bot se met en pause (et continue de staker) quand le plafond de coût est atteint, quand un
rééquilibrage reste bloqué 2 h, après 3 paires ratées d'affilée, si une position reste listée après 8 tentatives de
fermeture (cette pause se lève d'elle-même si la position finit par disparaître), ou si les soldes bougent alors que
l'API ne montre aucune position. Le motif est dans `data/bot.log`. Corrigez, puis `python -m ptbot resume`.

**Ne stakez pas à la main pendant un cycle** : le PAPER du cycle est mesuré sur le solde non staké, un stake manuel
le fausserait. `stake-now` refuse tant qu'un cycle est en cours ; arrêtez d'abord le bot (fichier `STOP`).

## Paramètres (.env)

| Variable | Défaut | Rôle |
|---|---|---|
| `LEVERAGE` / `MAX_LEVERAGE` | 250 / 250 | levier visé / levier max autorisé pour rester au notionnel minimum |
| `TARGET_NOTIONAL_USD` | 11000 | marge par jambe = notionnel visé / levier (44 $ en x250) ; 0 = tout le solde |
| `CLOSE_MOVE_PCT` | 0.70 | mouvement de prix qui déclenche la fermeture des 2 jambes |
| `LIQ_SAFETY_PCT` | 0.15 | écart minimum gardé avant la liquidation (borne `CLOSE_MOVE_PCT`) |
| `MARGIN_FRACTION` / `MAX_MARGIN_USD` | 1.0 / 0 | marge par jambe = plus petit solde × fraction (plafonnée) |
| `REBALANCE_THRESHOLD_PCT` / `MIN_REBALANCE_USD` | 10 / 5 | quand rééquilibrer |
| `STAKE_THRESHOLD_PAPER` | 1000 | seuil de staking automatique |
| `MAX_COST_PER_1000_PAPER` / `COST_WINDOW` | 7 / 1 | arrêt dès qu'un cycle (ou la moyenne de N cycles) dépasse le plafond |
| `OPEN_DEADLINE_S` | 120 | durée de validité d'un ordre d'ouverture (à monter si le relayer est lent) |

Un `CLOSE_MOVE_PCT` plus grand coûte moins cher par PAPER (le haircut baisse quand le mouvement grandit), mais
laisse moins de marge avant la liquidation, et les cycles durent plus longtemps.

## Comment ça marche techniquement

- **Ordres** : intents EIP-712 signés localement et envoyés à `POST https://exchange.papertrade.xyz/intents`
  (gasless). Ouverture/fermeture signées par une session key enregistrée par le wallet ; stake, retrait et
  annulation signés par le wallet principal. Les session keys sont demandées pour 6 jours et renouvelées entre deux
  cycles, 2 jours avant leur échéance. Les 4 clés précédentes encore valides sont gardées en secours (si le relayer
  traîne, celle qu'il active n'est pas forcément la dernière) ; une clé refusée en pleine fermeture déclenche un
  nouvel enregistrement, au plus un par minute.
- **Prix** : BBO mid Hyperliquid (`l2Book`), le même prix que PaperTrade utilise pour ouvrir et fermer.
- **Soldes** : lus on-chain sur HyperEVM (`balances(address)` de l'Exchange, `balanceOf` du token PAPER).
- **Rééquilibrage** : intent `withdrawToCore` du wallet riche → USDC sur HyperCore → `spotSend` vers l'adresse de
  dépôt PaperTrade de l'autre wallet → le bot attend que le solde soit crédité.
- **Coût d'un cycle** = (USDC + file d'attente) avant − après ; PAPER = solde PAPER après − avant.

Fichiers locaux : `data/bot.log` (journal), `data/state.json` (état et historique), `data/session_keys.json`
(clés de session, **secret**, permissions 600).

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

Signatures EIP-712, logique de décision, parseur de positions, et scénarios de bout en bout contre le simulateur :
cycle normal, staking, rééquilibrage, gap au-delà de la liquidation, plafond de coût, jambe refusée, réponse
perdue, positions invisibles, reprise après redémarrage. `tests/test_faults.py` couvre les pannes : réponses
perdues, exécutions tardives, ordres jamais exécutés, lectures d'API vides ou en retard, positions fantômes, crash
entre un ordre et la sauvegarde, transferts et stakes lents.

## Sources

- Post de [@rusk0f](https://x.com/rusk0f/status/2109921167830389061) (stratégie, x100, pas de liquidation, plafond 7 $/1 000).
- [arbitron-adm2/papertrade-client](https://github.com/arbitron-adm2/papertrade-client) : endpoints, types et domaines EIP-712.
- [giona20/papertrade-bot](https://github.com/giona20/papertrade-bot) : adresses des contrats, getters on-chain, formule d'émission.
- Haircut du simulateur calé sur les mesures publiées après le lancement (~48 % conservé à 0,03 %, ~74 % à 0,1 %, ~88 % à 1 %).

Logiciel expérimental, pas un conseil financier.
