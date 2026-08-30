# Cycle de vie UX des références — alpha.5

## Écrans

La navigation **Références** ouvre une bibliothèque volontairement compacte. Une card
présente le nom, l’application, la disponibilité, la version active, la taille, la date de
mise à jour et le nombre de versions. Les seules actions courantes sont **Gérer** et
**Déployer**. Toutes les actions destructrices sont dans la fiche.

La fiche `/reference-images/{image_id}` présente la version active, la source initiale,
les métadonnées et l’historique. Les états internes sont traduits en `ACTIVE`,
`HISTORIQUE`, `EN CONSTRUCTION`, `EN SUPPRESSION` ou `ERREUR`. Les identifiants,
checksums, snapshots et chemins restent repliés dans les détails techniques.

## Créer une référence

**Créer une référence** ouvre un wizard en cinq étapes : type, source, instance,
informations et validation. Alpha.5 active Plex depuis un node/serveur existant ou une
AppBox existante. Ces deux choix utilisent le même moteur : l’AppBox est simplement une
instance conteneur explicitement désignée. Jellyfin et l’import d’une archive depuis le
navigateur sont affichés comme indisponibles tant que leurs moteurs ne sont pas livrés.

À la validation, le Control Plane crée un `reference_build`, lance la découverte, puis le
préflight, la capture live non intrusive, l’upload, la validation et la publication déjà
existants. `/reference-builds/{build_id}` rend la progression et les logs observables.

## Créer une nouvelle version

Depuis une fiche, **Créer une nouvelle version** ouvre le même wizard avec
`target_image_id` fixé. `reference_builds.image_id` est le contrat explicite de ciblage :
la publication ne recalcule pas l’image depuis le nom. Elle ajoute une
`reference_image_version`, conserve le même `reference_images.image_id`, puis bascule
atomiquement `current_version_id`. Le nom, la description et la source initiale de la
référence ne sont pas remplacés par ceux du build. L’ancienne version reste publiée mais
apparaît comme historique et peut être réactivée avec confirmation.

Une AppBox déjà provisionnée n’est ni modifiée ni redéployée par cette bascule. Elle
conserve la version qu’elle a consommée jusqu’à un choix opérateur explicite.

## Suppression et résolution

La version active ne propose pas de suppression : l’interface demande de créer ou
d’activer une autre version. Une version historique ouvre le plan sécurisé existant. Les
blockers proposent une résolution : nouvelle version, profil, job, déploiement ou
statut de distribution. La suppression complète se trouve uniquement dans la zone de
danger de la fiche et conserve la confirmation forte par nom.

## Recette E2E ARTEMIS

1. Ouvrir **Références**, puis **E2E Plex ab34ah live capture**.
2. Vérifier que la version `2026-08-27-114` porte le badge `ACTIVE`.
3. Cliquer **Créer une nouvelle version**.
4. Choisir **AppBox existante**, node **ARTEMIS**, instance `plex-appb-34ah`.
5. Parcourir la validation et lancer la capture.
6. Sur le suivi, vérifier découverte, préflight, capture, validation et publication sans
   arrêt/restart du conteneur source.
7. Revenir à la fiche : la nouvelle version doit être `ACTIVE` et
   `2026-08-27-114` doit être `HISTORIQUE`.
8. Vérifier que l’AppBox source est toujours running, avec le même RestartCount.
9. Ouvrir **Supprimer cette version** sur `2026-08-27-114`, examiner le plan puis confirmer.
10. Vérifier l’archive centrale, les ACK caches, l’audit et l’état terminal de suppression.

Avant le test, sauvegarder la base et `APPBOX_REFERENCE_ROOT`. Ne pas utiliser DEMETER.
Un rollback UI/code restaure le commit précédent; une version ou archive effectivement
supprimée exige la restauration cohérente de la sauvegarde.
