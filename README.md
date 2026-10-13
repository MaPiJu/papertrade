# Bot delta neutral PaperTrade ($PAPER)

Bot qui farme du $PAPER sur [PaperTrade](https://papertrade.xyz) avec deux wallets, en reprenant la
méthode décrite par [@rusk0f](https://x.com/rusk0f/status/2109921167830389061) :

- **delta neutral sur 2 comptes** : le wallet A ouvre un long, le wallet B un short, même coin, même marge, levier x100 ;
- **jamais de liquidation** : dès que le prix a bougé de `CLOSE_MOVE_PCT` (0,70 % par défaut), le bot ferme
  **les deux jambes**, bien avant le prix de liquidation (0,95 % en x100). La jambe perdante réalise sa perte et
  mint du PAPER, la jambe gagnante récupère le mouvement moins le haircut de PaperTrade ;
- **staking automatique** : dès qu'un wallet détient 1 000 PAPER non stakés, le bot les stake, sans attendre de « go » ;
- **rééquilibrage des USDC** : après chaque cycle, un wallet a gagné et l'autre perdu. Le bot retire la moitié de
  l'écart du wallet riche vers HyperCore, puis l'envoie à l'adresse de dépôt PaperTrade de l'autre wallet ;
- **arrêt automatique** : dès qu'un cycle a coûté plus de 7 $ pour 1 000 PAPER, le bot arrête d'ouvrir des paires
  (l'émission baisse avec le temps), mais continue de staker.

```
idle ──► (rééquilibrage A⇄B si besoin) ──► ouverture long A + short B ──► surveillance du prix BBO Hyperliquid
  ▲                                                                              │ |mouvement| ≥ 0,70 %
  └── stake du PAPER ◄── bilan du cycle (coût $, PAPER mintés) ◄── fermeture des 2 jambes
```

## ⚠️ À lire avant de mettre de l'argent

- **API non officielle.** PaperTrade ne publie ni SDK ni ABI. Les endpoints, les types EIP-712 et les adresses
  viennent de la rétro-ingénierie du site par la communauté (10/10/2026). Ils peuvent changer sans prévenir.
  Le bot vérifie ce qu'il peut (domaines EIP-712 on-chain, minimums via `/state/trading`), mais **la lecture des
  positions ouvertes (`/query/accounts/{adresse}/trades`) n'a jamais pu être testée en conditions réelles** :
  lancez `probe` avant tout (voir plus bas).
- **Conditions d'utilisation.** En phase 1, PaperTrade réserve le trading au site et à des relayers whitelistés.
  Le bot envoie les mêmes intents signés que le site, au même relayer, mais automatiser peut être contraire aux
  CGU : c'est votre risque.
- **Capital et notionnel minimum.** Une position doit faire au moins 10 000 $ de notionnel. Avec 100 $ par wallet
  en x100, vous êtes **pile au minimum** : après le premier cycle (~9 $ de coût en simulation), les soldes
  repassent sous 100 $ et le bot ne peut plus ouvrir en x100. Il l'affiche et attend. Pour enchaîner les cycles,
  il faut soit plus d'USDC (150 $/wallet ≈ 2 à 9 cycles selon le haircut réel, 250 $/wallet environ le double), soit
  autoriser le levier à monter (`MAX_LEVERAGE=150` ou 200), ce qui rapproche la liquidation. Dans tous les cas,
  ~100 $ par wallet restent immobilisés comme marge minimale : seul l'excédent est « brûlé » pour minter.
- **Le coût par PAPER a augmenté.** rusk0f s'est arrêté quand un cycle dépassait 7 $/1 000 PAPER. Si l'émission
  actuelle est déjà basse, le plafond se déclenchera vite. `python -m ptbot plan` l'estime avant de lancer.
- **Risques du protocole.** Contrats non vérifiés et upgradables, LP unique, profits mis en file d'attente si la
  LP manque de liquidités, PAPER non transférable pour l'instant. Un gap de prix au-delà de 0,95 % entre deux
  lectures liquide la jambe perdante (le bot le gère, mais le cycle coûte plus cher).
- **Clés privées.** Le bot a besoin des clés des deux wallets : le staking et les retraits sont signés par le
  wallet principal. Utilisez deux wallets dédiés, ne mettez que ce que vous acceptez de perdre, gardez `.env` et
  `data/session_keys.json` pour vous.
- **Wallets dédiés.** Entre deux cycles, le bot **ferme toute position qu'il ne gère pas** sur A et B (un ordre
  exécuté en retard, une réponse perdue… ou un trade fait à la main). Ne tradez pas à la main sur ces wallets.

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

1. **`python -m ptbot probe`** (lecture seule). Affiche et enregistre dans `data/probe/probe.json` : soldes
   PaperTrade, PAPER, soldes HyperCore, adresses de dépôt (.env vs API), domaines EIP-712 on-chain, minimums,
   émission, et **les positions ouvertes telles que le bot les comprend**.
   Ouvrez une petite position à la main sur le site, relancez `probe`, et vérifiez qu'elle apparaît dans
   `open_positions_parsed`. Si elle n'apparaît pas, le format de l'API diffère de celui prévu dans
   `ptbot/positions.py` : envoyez-moi le contenu de `trades_raw_first_page` et j'adapte le parseur.
2. **`python -m ptbot sim --hours 72 --usdc 100`** : la stratégie en simulation hors-ligne (prix synthétiques,
   haircut et émission estimés). `--live-prices` utilise les vrais prix Hyperliquid en temps réel.
3. **`python -m ptbot plan`** : ce que le bot ferait maintenant (rééquilibrage, marge, levier, coût estimé).
4. **`python -m ptbot register`** : enregistre une session key par wallet (comme le site). Si c'est refusé
   (`403 unauthorized_intent`), les types EIP-712 ont changé : ne lancez pas `run`.
5. **`python -m ptbot run`** : lance le bot (confirmation demandée ; `--yes` pour `nohup`/systemd). Surveillez
   le premier cycle sur le site.

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
- réponse perdue ou erreur réseau sur un ordre : l'ordre est considéré comme « peut-être exécuté ». Le bot le
  surveille jusqu'à son expiration (2 min) et n'ouvre jamais une nouvelle paire avant ;
- une jambe ouverte seule : sa perte est surveillée et elle est fermée dès qu'elle atteint la distance de sortie,
  ou à l'expiration de l'autre ordre ;
- l'état est sauvegardé **avant** chaque ordre et chaque transfert : un crash ou un redémarrage reprend où il en
  était, sans rouvrir de paire ni retirer deux fois ;
- une lecture d'API vide ou en retard ne ferme pas la paire (3 lectures manquées d'affilée sont nécessaires) ;
- chaque jambe sort sur son propre prix d'entrée, même si les deux ont été exécutées à des prix différents ;
- les transferts lents (retrait vers HyperCore, crédit du dépôt) sont attendus jusqu'à 2 h, avec relance automatique
  si un retrait a expiré sans être exécuté.

**Pauses** : le bot se met en pause (et continue de staker) quand le plafond de coût est atteint, quand un
rééquilibrage reste bloqué 2 h, après 3 paires ratées d'affilée, si une position reste listée après 8 tentatives de
fermeture, ou si les soldes bougent alors que l'API ne montre aucune position. Le motif est dans `data/bot.log`.
Corrigez, puis `python -m ptbot resume`.

## Paramètres (.env)

| Variable | Défaut | Rôle |
|---|---|---|
| `LEVERAGE` / `MAX_LEVERAGE` | 100 / 100 | levier visé / levier max autorisé pour rester au notionnel minimum |
| `CLOSE_MOVE_PCT` | 0.70 | mouvement de prix qui déclenche la fermeture des 2 jambes |
| `LIQ_SAFETY_PCT` | 0.15 | écart minimum gardé avant la liquidation (borne `CLOSE_MOVE_PCT`) |
| `MARGIN_FRACTION` / `MAX_MARGIN_USD` | 1.0 / 0 | marge par jambe = plus petit solde × fraction (plafonnée) |
| `REBALANCE_THRESHOLD_PCT` / `MIN_REBALANCE_USD` | 10 / 5 | quand rééquilibrer |
| `STAKE_THRESHOLD_PAPER` | 1000 | seuil de staking automatique |
| `MAX_COST_PER_1000_PAPER` / `COST_WINDOW` | 7 / 1 | arrêt dès qu'un cycle (ou la moyenne de N cycles) dépasse le plafond |

Un `CLOSE_MOVE_PCT` plus grand coûte moins cher par PAPER (le haircut baisse quand le mouvement grandit), mais
laisse moins de marge avant la liquidation, et les cycles durent plus longtemps.

## Comment ça marche techniquement

- **Ordres** : intents EIP-712 signés localement et envoyés à `POST https://exchange.papertrade.xyz/intents`
  (gasless). Ouverture/fermeture signées par une session key enregistrée par le wallet ; stake et retrait
  signés par le wallet principal.
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
