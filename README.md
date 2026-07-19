# Gestion de stock — Django / MySQL

Application de gestion de stock, ventes et caisse pour une distribution
multi-clients avec tarifs differencies.

## Modules

| App | Rôle |
|-----|------|
| `core` | Base commune (modèle horodaté, utilitaires, initialisation des rôles) + tableau de bord |
| `clients` | Catégories, listes de tarifs, clients + vues de consultation |
| `catalogue` | Types d'articles, conditionnements, articles, tarifs par liste |
| `stock` | Mouvements, approvisionnements, sorties (vol/casse/offert), inventaires |
| `ventes` | Commandes, factures, paiements, encaissement, état de suivi client, PDF |
| `caisse` | Clôture journalière (auto + manuelle) |
| `dashboard` | Application de l'interface (charte, layout) |
| `api` | API REST du stock en temps réel (Django REST Framework) |

## Principes de conception

- **Le stock dérive toujours des mouvements** (`MouvementStock`). On n'écrit
  jamais un stock « à la main » : tout passe par un approvisionnement, une
  vente, une sortie exceptionnelle ou un inventaire. `Article.stock_actuel`
  renvoie la somme signée des mouvements → cohérent et consultable en temps
  réel par plusieurs utilisateurs simultanément.
- **Conditionnement** : le stock est compté en unités de base. Un
  conditionnement (casier de 6, 12, 16…) porte son `nombre_unites`. Une ligne
  de 3 casiers de 12 = 36 unités déstockées.
- **Tarifs par liste** : un `Tarif` lie (liste de tarifs, article,
  conditionnement) → prix. Un même article vaut donc un prix différent selon
  la liste à laquelle appartient le client.
- **Cycle de vente** : une `Commande` reste un *bon de commande* tant qu'elle
  n'est pas validée. `commande.valider()` vérifie le stock, crée la `Facture`
  (avec copie des lignes et des prix), génère les sorties de stock et passe la
  commande en `VALIDEE`.
- **Factures inviolables** : `Facture.delete()` lève une exception ; l'admin
  interdit la suppression (y compris pour un superutilisateur). L'annulation
  se fait avec **justificatif obligatoire** (`facture.annuler`), tracée
  (auteur, date), et **restaure le stock**.
- **Paiements** : `COMPTANT` ou `ACOMPTE`. Le reliquat = total − payé, calculé
  automatiquement ; un paiement ne peut pas dépasser le reliquat.

## Installation (développement)

```bash
python -m venv .venv
source .venv/bin/activate            # Windows : .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                 # DB_ENGINE=sqlite par défaut
python manage.py migrate
python manage.py init_roles          # crée les profils (groupes)
python manage.py createsuperuser
python manage.py runserver
```

- Interface métier : http://127.0.0.1:8000/ (connexion via `/connexion/`)
- Administration Django : http://127.0.0.1:8000/admin/

## Production (cPanel / MySQL)

Dans `.env`, passer `DB_ENGINE=mysql` et renseigner `DB_*`. `mysqlclient` est
déjà dans `requirements.txt`. Le fichier `config/wsgi.py` est prêt pour
Passenger.

### Clôture de caisse automatique (cron)

À chaque fin de journée, programmer dans le gestionnaire de tâches cron cPanel :

```
55 23 * * *  /home/USER/virtualenv/.../bin/python /home/USER/app/manage.py cloturer_caisse
```

Si le cron tourne après minuit, utiliser `cloturer_caisse --hier`.

## Profils (rôles)

`python manage.py init_roles` crée quatre groupes :

- **Administrateur** — paramétrage complet (clients, catalogue, tarifs, stock).
  Ne peut pas supprimer de facture.
- **Gestionnaire** — catalogue, stock, approvisionnements, inventaires.
- **Vendeur** — commandes et validation en facture ; peut annuler (justificatif).
- **Caissier** — encaissements et clôture de caisse ; peut annuler (justificatif).

L'annulation de facture est volontairement réservée aux profils
non-administrateurs, conformément au besoin exprimé.

## Import d'inventaire physique

```bash
python manage.py importer_inventaire comptage.csv --date 2026-07-06 --periode MOIS --valider
```

CSV attendu (séparateur `;`, en-tête inclus) :

```
code_article;quantite_physique
A001;80
A002;134
```

`--valider` calcule les écarts (physique − théorique) et génère les
ajustements de stock. Sans cette option, l'inventaire est créé en brouillon
et se valide ensuite depuis l'admin.

## État de suivi par client

`ventes.services.suivi_client(client, periode, reference)` renvoie, sur une
période (`JOUR`, `SEMAINE`, `QUINZAINE`, `MOIS`, `TRIMESTRE`, `SEMESTRE`,
`ANNEE`), la liste des factures avec date, montant, payé, reliquat, statut et
date du dernier paiement, ainsi que les totaux.

## Interface web (Bootstrap 5)

L'interface reprend la **charte graphique dérivée du logo** (rouge brique,
olive, terracotta, crème ; typographies Poppins/Inter), définie dans
`static/css/app.css`. Layout à barre latérale responsive (`templates/base.html`).

| Écran | URL | Contenu |
|-------|-----|---------|
| Tableau de bord | `/` | CA du jour/mois, bons en attente, dernières factures, alertes de stock |
| Saisie de commande | `/ventes/commandes/nouvelle/` | Sélection client → lignes dynamiques (article/conditionnement/quantité), **prix injecté depuis la liste de tarifs du client**, total en direct |
| Détail commande | `/ventes/commandes/<id>/` | Validation → facture, suppression d'un bon |
| Factures | `/ventes/factures/` | Liste filtrable (payée / partielle / non payée / annulée) |
| Détail facture | `/ventes/factures/<id>/` | **Encaissement** (comptant/acompte, mode), annulation avec justificatif, lien PDF |
| Suivi client | `/ventes/suivi-client/` | **État filtrable par période** (jour → année) : facturé, payé, reliquat |
| État du stock | `/stock/` | Niveaux **temps réel**, valeur au prix d'achat, alertes ; filtre « sous seuil » |
| Caisse | `/caisse/` | Historique des clôtures + clôture manuelle |
| Clients / Articles | `/clients/`, `/catalogue/articles/` | Consultation filtrable, fiches |

La saisie/édition de référentiel (clients, articles, tarifs, appros…) se fait
via l'admin Django, relié depuis chaque écran.

### Règle prix-plancher

Un prix de vente ne peut jamais être **inférieur au prix d'achat**
(`Article.prix_achat`, mis à jour à la validation d'un approvisionnement
chiffré). La règle est verrouillée à trois niveaux : `Tarif.clean()`,
`LigneCommande.save()` (saisie manuelle) et `Commande.valider()` (contrôle
final avant facturation).

## PDF des factures (WeasyPrint)

`GET /ventes/factures/<id>/pdf/` génère la facture en PDF (charte, logo,
lignes, total, reliquat, règlements). Si WeasyPrint (ou ses bibliothèques
système) est indisponible, la vue **retombe automatiquement sur une version
HTML imprimable** plutôt que d'échouer.

> **Déploiement cPanel** : WeasyPrint nécessite Pango, Cairo et GDK-PixBuf.
> Si l'hébergement ne les fournit pas, le repli HTML reste fonctionnel
> (impression navigateur → PDF). La version de `pydyf` est **épinglée à
> 0.10.0** dans `requirements.txt` : les versions ≥ 0.11 cassent WeasyPrint 60.2.

## API stock temps réel (Django REST Framework)

Authentifiée par session (mêmes identifiants que l'interface).

| Endpoint | Retour |
|----------|--------|
| `GET /api/stock/` | Tous les articles actifs avec `stock_actuel` (recalculé depuis les mouvements) et `sous_seuil` |
| `GET /api/stock/<id>/` | Un article |
| `GET /api/stock/alertes/` | Uniquement les articles sous le seuil |

L'écran **État du stock** interroge `/api/stock/` toutes les 20 s et met à jour
les niveaux sans rechargement — cohérent en multi-postes puisque le stock est
toujours dérivé des mouvements.

## Tests

Smoke-test complet passé (41/41), client de test authentifié :

- **Logique métier** : appro chiffré → prix d'achat (1 500/unité) → stock
  (120 unités) ; règle prix-plancher qui **refuse** un tarif sous le prix
  d'achat ; commande 2 lignes → validation → facture → **stock décrémenté**
  (120 → 91) ; encaissement d'acompte → reliquat recalculé.
- **Rendu** : tableau de bord, listes filtrables (clients, articles, factures),
  détails, état du stock (+ filtre alerte), appros, sorties, inventaires,
  caisse, saisie de commande, suivi client, `ajax/tarifs`, API DRF
  (`/api/stock/`, `/alertes/`) — tous en 200.
- **PDF** : `facture_pdf` génère un vrai PDF (`application/pdf`) quand WeasyPrint
  est présent ; repli HTML sinon.

Rejouable via `python smoke_ui.py` (non inclus dans le livrable).

## Hypothèses retenues

À ajuster selon ton besoin réel :

- Le stock est suivi en **unités de base** ; les casiers sont des
  conditionnements. Une vente peut se faire au casier ou à l'unité.
- La caisse enregistre l'**encaissé du jour** (par date de paiement), le
  **facturé du jour** et le **reliquat du jour**, plus un reliquat global cumulé.
- « L'administrateur ne peut pas supprimer » a été étendu à **personne ne
  supprime** une facture ; l'annulation (non-admin, avec justificatif) remplace
  la suppression.
