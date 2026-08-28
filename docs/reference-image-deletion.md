# Suppression sûre des Reference Images — alpha.5

## Utilisation

Dans **Images de référence**, choisir **Supprimer**. Une page de confirmation affiche
le nom, le nombre total de versions et les dépendances. La confirmation porte sur
l'état affiché : toute modification des versions/artefacts impose de recharger la page.
La suppression est définitive ; sauvegarder la base et les archives avant utilisation.
Aucun force-delete et aucune commande de suppression distante.

API : `GET /api/reference-images/{image_id}/deletion` donne `confirmation`, `blockers`,
`version_count` et `state`. Envoyer ensuite `DELETE /api/reference-images/{image_id}`
avec `{"confirmation":"<valeur reçue>"}`. Confirmation absente : 422 ; référence modifiée
ou dépendances présentes : 409 ; image jamais connue : 404. La confirmation est une
protection contre les changements concurrents, pas un mécanisme d'authentification/CSRF.
Les règles d'accès opérateur existantes s'appliquent ; leur durcissement reste un sujet séparé.

## Dépendances bloquantes

- AppBox liée par image, version **ou snapshot**, quel que soit son état.
- Déploiement Control Plane ou historique snapshot référençant une version/snapshot.
  Alpha.5 conserve volontairement tous ces déploiements, même échoués : aucun détachement
  automatique d'un historique. Le refus indique ID et statut.
- Profil de provisioning lié, même désactivé ; snapshot ou archive partagé avec une autre image.
- Build actif rattaché à l'image/version ; distribution `transferring`.
- Chemin d'artefact hors de `APPBOX_REFERENCE_ROOT`, relatif, contenant `..`, lien,
  jonction ou répertoire au lieu d'un fichier.

Le contrôle est refait dans une transaction SQLite `BEGIN IMMEDIATE`. Des triggers
sur les colonnes historiques sans FK d'AppBox/profil refusent les écrivains tardifs
référençant une image/version supprimée ou un snapshot marqué deleted.

## Base, archives et reprise

Une table additive `reference_image_deletions` est créée au démarrage du CP, sans
migration manuelle. Elle conserve identité, versions, snapshots sources, liens de builds,
caches distants orphelins, distributions, liste exacte des fichiers et état du nettoyage.
Sauvegarder la base avant déploiement. Les versions sont supprimées par cascade ; les
FK des builds passent à NULL selon le modèle existant, leurs statuts/logs sont conservés.
Un résultat de build déjà publié ne republie donc pas l'image après sa suppression.
Les snapshots sont conservés pour audit avec `status=deleted` et `source_path=NULL` ;
leur état original est enregistré dans le journal.

La transaction supprime le catalogue et crée **atomiquement** le journal de nettoyage.
Aucun fichier n'est supprimé avant son commit. Les archives déclarées par les versions,
leurs archives `deployment-cache` et marqueurs de capture sont ensuite supprimés,
individuellement, sans suppression récursive. Les sources externes importées restent intactes.
Les répertoires vides et fichiers non déclarés ne sont pas balayés.

Une panne disque renvoie **202 / cleanup_pending**, pas un faux succès. L'image n'est
plus proposée au provisioning ; la bibliothèque affiche « Nettoyage en attente ».
« Reprendre le nettoyage » rejoue le journal. Un fichier déjà absent est accepté,
y compris après une interruption entre unlink et commit. Le contrôle d'identité
(device/inode/taille/mtime) suspend le nettoyage si le fichier a été remplacé.
Un changement du storage root suspend aussi la reprise. Les erreurs sont affichées.
Réparer les permissions/le stockage puis reprendre ; ne pas supprimer le journal.

Après succès : **200 / deleted**. Répéter la même confirmation est sans effet. Un nouvel
usage du même image_id n'est possible qu'après nettoyage ; une ancienne confirmation
ne supprime pas une nouvelle image. Le journal reste un audit, pas une sauvegarde des archives.
Un retour arrière après suppression physique exige une sauvegarde cohérente base + archives.

Les chemins sont revérifiés à chaque reprise. Sous Linux, l'ouverture des répertoires
avec `O_NOFOLLOW` et l'unlink relatif à des descripteurs empêchent la redirection par
substitution d'un parent. Le storage configuré et ses parents doivent rester sous
contrôle administrateur. La génération locale des archives est sérialisée avec le
nettoyage pour éviter une écriture tardive du cache par ce processus CP.

## Caches distants et validation terrain

Les lignes de `node_reference_cache` deviennent des enregistrements historiques dans
`manifest_json.orphaned_node_cache`, sans FK vivante vers une version supprimée.
Les fichiers distants ne bougent pas. Aucun node n'a besoin d'être online. Le nettoyage
physique distant éventuel sera une opération séparée, explicitement autorisée.

Tests locaux : catalogue vide/multiversion, dépendances, archives absentes, chemins
invalides/liens simulés, FK, échec DB avant unlink, erreur disque, interruption/reprise,
réutilisation d'identifiant, confirmation et blockers UI. Avant utilisation réelle,
valider les permissions/montages Linux, les descripteurs `O_NOFOLLOW`, une interruption
processus et les fichiers ouverts sur le stockage cible. Ces validations n'ont pas été exécutées.
