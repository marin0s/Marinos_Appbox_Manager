# V0.4.2.2 — Correctif renforcé popup

Le symptôme montrait que l’ancien JavaScript continuait d’être utilisé malgré le correctif précédent.

## Corrections

- Nouveau fichier statique unique : `app-v0.4.2.2.js`.
- Suppression complète de l’ancien dossier applicatif pendant l’upgrade.
- Reconstruction Docker avec `--no-cache`.
- Vérification automatique que la page charge bien le nouvel asset.
- Les étapes passent toutes à `TERMINÉ` dès que :
  - le job est en `success`, ou
  - sa progression atteint 100 %.
- La croix de fermeture n’est plus désactivée.
- Fermer la popup n’interrompt pas le job : il continue côté backend.
- La popup affiche `UI 0.4.2.2`, ce qui permet de confirmer immédiatement la version réellement chargée.

## Installation

```bash
cd /root
unzip marinos-appbox-manager-v0.4.2.2-artemis.zip
cd appbox-manager-poc-v0.4.2.2
chmod 755 upgrade-v0.4.2.2-artemis.sh
./upgrade-v0.4.2.2-artemis.sh
```

Après installation, ouvrir une nouvelle fenêtre privée ou faire `Ctrl+F5`.

Dans la popup, la ligne de sous-titre doit contenir :

```text
UI 0.4.2.2
```
