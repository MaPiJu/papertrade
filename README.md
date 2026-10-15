# Bot delta neutral PaperTrade ($PAPER)

Bot qui farme du $PAPER sur [PaperTrade](https://papertrade.xyz) en delta neutral (un long et un short BTC de même
taille), d'après la méthode de [@rusk0f](https://x.com/rusk0f/status/2109921167830389061). PaperTrade mint du PAPER
sur les pertes et prélève une retenue (environ 13 % autour de 2 %) sur les gains : le coût d'un PAPER ne dépend
que de l'ampleur du mouvement sur lequel le gain est encaissé (formule vérifiée au centime sur de vrais trades).

Deux stratégies :

- **échelle (`STRATEGY=ladder`, par défaut)** : quand une jambe perd `LADDER_STEP_PCT` (0,25 %), le bot la ferme
  (sa perte mint du PAPER) et la rouvre aussitôt au prix du moment, avec la même quantité de BTC que l'autre jambe.
  L'autre jambe continue de courir. Quand une jambe gagne `LADDER_RESET_PCT` (calculé à partir de l'émission pour
  rester sous le plafond de coût, ~2 % aujourd'hui), le bot ferme tout et recommence. Le PAPER est minté par petites
  marches, et le gain, seul à subir la retenue, est encaissé sur un grand mouvement : même coût par PAPER que la
  paire, mais des périodes environ deux fois plus courtes (elles s'arrêtent quand l'écart entre le plus haut et le
  plus bas atteint ~2 %, pas quand le prix s'est éloigné de 2 % de son départ) ;
- **paire (`STRATEGY=pair`)** : le wallet A ouvre un long, le wallet B un short ; dès que le prix a bougé de
  `CLOSE_MOVE_PCT`, le bot ferme les deux jambes ;
- **un seul wallet (`ONE_WALLET=true`, conseillé avec l'échelle et un petit capital)** : le long et le short vivent
  sur le wallet A. Le bot y transfère une fois l'USDC de B, puis plus aucun rééquilibrage : tout le capital finance la
  jambe qui perd, d'où des positions environ deux fois plus grosses qu'avec deux wallets. Un test réel préalable
  (`--straddle`) vérifie que PaperTrade garde bien les deux positions séparées.

Simulation du bot complet sur 72 h (BTC à 0,5 %/h de volatilité, émission 18,9 PAPER/$, ~109 $ au départ) :

| Stratégie | PAPER par jour | Coût pour 1 000 PAPER |
|---|---|---|
| paire x40, fermeture à ±2 % (deux wallets) | ~520 à 990 | 6,86 $ |
| échelle, deux wallets | ~370 à 980 (bloquée sous ~43 $ par wallet) | ~6,9 $ |
| échelle, un seul wallet | ~1 900 à 2 100 | ~6,8 $ |

Dans tous les cas :

- **jamais de liquidation** : chaque jambe est fermée au moins `LIQ_SAFETY_PCT` (0,15 %) avant son prix de
  liquidation ;
- **staking automatique** : dès qu'un wallet détient 1 000 PAPER non stakés, le bot les stake, sans attendre de « go » ;
- **rééquilibrage des USDC** (deux wallets) : après chaque cycle, le bot retire la moitié de l'écart du wallet riche
  vers HyperCore, puis l'envoie à l'adresse de dépôt PaperTrade de l'autre wallet ;
- **arrêt automatique** : dès que le coût moyen des `COST_WINDOW` derniers cycles dépasse 7 $ pour 1 000 PAPER, le
  bot arrête d'ouvrir des positions (l'émission baisse avec le temps), mais continue de staker. Le coût cumulé de
  tous les cycles (avortés compris) est aussi surveillé. Un cycle écourté (réouverture refusée, solde insuffisant,
  `close-cycle`, session key bientôt expirée) ne compte que dans ce cumul : encaissé sur un petit mouvement, il coûte
  plus cher sans rien dire de la rentabilité du farming.

```
idle ──► (transfert B→A ou rééquilibrage) ──► ouverture long + short ──► surveillance du prix BBO Hyperliquid
  ▲                                                                       │ échelle: perte ≥ 0,25 % → réarmement
  │                                                                       │ (fermer + rouvrir la jambe perdante)
  └── stake du PAPER ◄── bilan (coût $, PAPER mintés) ◄── fermeture des 2 jambes ◄── gain ≥ ~2 % (échelle) ou
                                                                                     mouvement ≥ CLOSE_MOVE (paire)
```

## ⚠️ À lire avant de mettre de l'argent

- **API non officielle.** PaperTrade ne publie ni SDK ni ABI. Les endpoints, les types EIP-712 et les adresses
  viennent de la rétro-ingénierie du site par la communauté (10/10/2026). Ils peuvent changer sans prévenir.
  Le bot vérifie ce qu'il peut (domaines EIP-712 on-chain, minimums via `/state/trading`), mais **la lecture des
  positions ouvertes (flux `/state/user/live`, celui du site ; l'historique `/trades` ne liste que les positions
  fermées) n'a pas encore été testée sur vos propres positions** :
  suivez la mise en route ci-dessous, `run` refuse de démarrer sans les mini-positions de test.
- **Conditions d'utilisation.** En phase 1, PaperTrade réserve le trading au site et à des relayers whitelistés,
  et contourner le site « expose à une exclusion ». Le bot envoie les mêmes intents signés que le site, au même
  relayer, en s'identifiant honnêtement, mais c'est de l'automatisation : c'est votre risque.
- **Capital et notionnel minimum.** Une position doit faire au moins 1 000 $ de notionnel et 10 $ de marge. L'échelle
  dimensionne ses positions pour que la jambe qui perd puisse payer une période entière de marches ; sous ~43 $ par
  wallet (deux wallets) ou ~55 $ (un wallet) elle n'est plus possible et le bot passe à la paire classique.
- **Retenue et frais vérifiés.** La jambe perdante perd exactement notionnel × mouvement (PAPER minté sur 98 % de la
  perte) ; la gagnante reçoit son gain après la retenue de la doc (« asymmetric impact ») et 2 % de frais. Les
  positions longues de plusieurs heures n'ont montré ni frais d'ouverture ni funding. Si la file d'attente des
  profits de PaperTrade est active, le gain de la jambe gagnante n'est pas disponible tout de suite.
- **Le coût par PAPER a augmenté.** rusk0f s'est arrêté quand un cycle dépassait 7 $/1 000 PAPER. Si l'émission
  actuelle est déjà basse, le plafond se déclenchera vite. `python -m ptbot plan` l'estime avant de lancer.
- **Risques du protocole.** Contrats non vérifiés et upgradables, LP unique, profits mis en file d'attente si la
  LP manque de liquidités, PAPER non transférable pour l'instant. Un saut de prix plus grand que l'écart gardé avant la
  liquidation (0,15 %) pendant la dizaine de secondes d'une fermeture liquide la jambe perdante (le bot le gère, mais
  la période coûte plus cher).
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
   Tout `FAIL` est bloquant. Il faut notamment : décalage d'horloge < 5 s, minimums `(10.0, 1000.0)` ou
   `(10.0, 10000.0)`, sur les comptes publics `ids N/N` sans `unknown`, `balances() return
   size 32 bytes`, adresses de dépôt API = `.env`. Si `profit queue` est actif, `userQueuedTotal` doit être lisible.
3. **`python -m ptbot probe`**, puis ouvrez `data/probe/probe.json` : soldes égaux à
   ceux du site, `usdc_queued` est un nombre, `open_positions_live` vide (`[]`) si vous n'avez rien
   d'ouvert.
4. **Position manuelle sur le site** (bot arrêté, avant d'enregistrer la clé du bot) : sur A, la plus petite
   position possible : 10 $ en x100 si `probe` affiche un minimum de 1 000 $ (`minimums_usd`), en x1000
   s'il affiche 10 000 $. Pendant qu'elle est ouverte, relancez `probe` : `open_positions_live` doit la
   contenir (bon id, BTC, sens, prix d'entrée) et `usdc_papertrade` doit avoir baissé d'environ 10 $. Fermez-la (ou
   laissez-la liquider) et relancez `probe` : elle doit avoir disparu. **Sinon, n'allez pas plus loin** et
   envoyez-moi `account_live_raw` (dans `data/probe/probe.json`) pour adapter `ptbot/positions.py`.
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
   `python tools/preflight_tiny_trade.py --wallet A --i-understand-this-trades`, et pareil pour B (pas besoin de B
   avec `ONE_WALLET=true`). Toutes les lignes doivent être `PASS` ; notez les latences. Si l'ouverture met plus de
   60 s, montez `OPEN_DEADLINE_S`. L'échelle ne garde que 0,15 % entre une marche et la liquidation : si la fermeture
   met plus de 10 s, montez `LIQ_SAFETY_PCT` (le levier baisse d'autant) avant d'aller plus loin.
   **Avec `ONE_WALLET=true`**, faites aussi le test long + short sur le même wallet (2 × 10 $) :
   `python tools/preflight_tiny_trade.py --wallet A --straddle --i-understand-this-trades`. Il doit afficher
   « long and short coexist as two positions on one wallet » en `PASS` ; sinon, restez sur deux wallets.
9. **`python -m ptbot plan`** : stratégie, émission, taille des positions, marche et seuil d'encaissement, PAPER et
   coût attendus par période (formule vérifiée), comparés au plafond.
10. **Premier lancement supervisé** dans `tmux` (le bot doit survivre à la fermeture du terminal) :
    `python -m ptbot run` et `tail -f data/bot.log`. Surveillez avec `python -m ptbot status` (pas avec le site).
    Vérifiez « Paire ouverte », les lignes « je la ferme … et la rouvre » puis « Jambe … réarmée », l'absence de
    « refusé », et le bilan de la première période (coût pour 1 000 PAPER proche de celui annoncé par `plan`).
11. **Laissez-le tourner seul après au moins 3 périodes propres** (coûts et PAPER cohérents avec le site).
    Refaites l'étape 2 après toute mise à jour de PaperTrade (contrats upgradables).

**Changer de stratégie** : modifiez `.env` puis redémarrez le bot. Un cycle en cours se termine selon la stratégie
avec laquelle il a été ouvert ; la nouvelle s'applique au suivant.

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
| `STRATEGY` | ladder | `ladder` (échelle) ou `pair` (paire classique) |
| `ONE_WALLET` | false | long et short sur le wallet A, l'USDC de B y est transféré une fois (test `--straddle` requis) |
| `LADDER_STEP_PCT` | 0.25 | échelle : perte à laquelle une jambe est fermée et rouverte (le levier en découle) |
| `LADDER_RESET_PCT` | 0 | échelle : gain qui termine la période ; 0 = calculé depuis l'émission pour rester 2 % sous le plafond |
| `LADDER_MAX_LEVERAGE` | 1000 | échelle : levier maximum des jambes |
| `LEVERAGE` / `MAX_LEVERAGE` | 250 / 250 | paire : levier visé / levier max autorisé pour rester au notionnel minimum |
| `TARGET_NOTIONAL_USD` | 11000 | paire : marge par jambe = notionnel visé / levier (44 $ en x250) ; 0 = tout le solde |
| `CLOSE_MOVE_PCT` | 0.70 | paire : mouvement de prix qui déclenche la fermeture des 2 jambes |
| `LIQ_SAFETY_PCT` | 0.15 | écart minimum gardé avant la liquidation (borne `CLOSE_MOVE_PCT`, fixe le levier de l'échelle) |
| `MARGIN_FRACTION` / `MAX_MARGIN_USD` | 1.0 / 0 | marge par jambe = plus petit solde × fraction (plafonnée) |
| `REBALANCE_THRESHOLD_PCT` / `MIN_REBALANCE_USD` | 10 / 11 | quand rééquilibrer (11 minimum : retraits et dépôts de 10 USDC, +1 USDC d'activation HyperCore au premier retrait) |
| `STAKE_THRESHOLD_PAPER` | 1000 | seuil de staking automatique |
| `MAX_COST_PER_1000_PAPER` / `COST_WINDOW` | 7 / 1 | arrêt dès qu'un cycle (ou la moyenne de N cycles) dépasse le plafond ; 3 conseillé avec l'échelle (le coût d'une période varie de quelques %) |
| `OPEN_DEADLINE_S` | 120 | durée de validité d'un ordre d'ouverture (à monter si le relayer est lent) |

Le coût par PAPER ne dépend que du mouvement sur lequel le gain est encaissé (`CLOSE_MOVE_PCT` pour la paire,
`LADDER_RESET_PCT` pour l'échelle) : plus il est grand, moins le PAPER coûte, mais plus les cycles sont longs. À
l'émission 18,9 : 0,20 % → ~11 $/1 000, 0,70 % → ~7,8 $, 1 % → ~7,4 $, 2 % → ~6,9 $, 3 % → ~6,7 $, jamais moins de
~6,4 $. Le pas de l'échelle (`LADDER_STEP_PCT`) ne change pas ce coût : il fixe le levier et la finesse des marches.

## Comment ça marche techniquement

- **Ordres** : intents EIP-712 signés localement et envoyés à `POST https://exchange.papertrade.xyz/intents`
  (gasless). Ouverture/fermeture signées par une session key enregistrée par le wallet ; stake, retrait et
  annulation signés par le wallet principal. Les session keys sont demandées pour 6 jours et renouvelées entre deux
  cycles, 2 jours avant leur échéance ; un cycle encore ouvert 1 jour avant l'échéance est fermé pour permettre le
  renouvellement. Les 4 clés précédentes encore valides sont gardées en secours (si le relayer
  traîne, celle qu'il active n'est pas forcément la dernière) ; une clé refusée en pleine fermeture déclenche un
  nouvel enregistrement, au plus un par minute.
- **Prix** : BBO mid Hyperliquid (`l2Book`), le même prix que PaperTrade utilise pour ouvrir et fermer.
- **Soldes** : lus on-chain sur HyperEVM (`balances(address)` de l'Exchange, `balanceOf` du token PAPER).
- **Rééquilibrage** : intent `withdrawToCore` du wallet riche → USDC sur HyperCore → `spotSend` vers l'adresse de
  dépôt PaperTrade de l'autre wallet → le bot attend que le solde soit crédité.
- **Coût d'un cycle** = (USDC + file d'attente) avant − après ; PAPER = solde PAPER après − avant. Pour l'échelle, le
  cycle est la période entière (tous les réarmements compris).
- **Réarmement** : fermeture de l'ancienne position et ouverture de la nouvelle envoyées l'une après l'autre (la paire
  reste couverte), nouvelle jambe de même quantité de BTC que l'autre ; la nouvelle position est reconnue à son sens,
  sa marge et son levier, l'ancienne n'est oubliée qu'après 3 lectures sans elle.
- **Émission** : celle du site (`/query/protocol/summary`), à défaut les getters on-chain.

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
