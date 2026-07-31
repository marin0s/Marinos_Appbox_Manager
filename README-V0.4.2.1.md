# V0.4.2.1 — Correctif popup de progression

Corrige l’incohérence où un job terminé à 100 % pouvait conserver toutes ses étapes en « EN ATTENTE ».

Modifications :
- un job `success` force toutes les étapes en `TERMINÉ` ;
- le nombre d’étapes terminées est calculé explicitement ;
- affichage distinct de l’étape active et de l’étape en erreur ;
- cache busting CSS/JavaScript pour éviter que le navigateur conserve l’ancien fichier ;
- version applicative `0.4.2.1`.

Installation :

```bash
cd /root
unzip marinos-appbox-manager-v0.4.2.1-artemis.zip
cd appbox-manager-poc-v0.4.2.1
chmod 755 upgrade-v0.4.2.1-artemis.sh
./upgrade-v0.4.2.1-artemis.sh
```

Après installation, effectuer une recharge forcée avec `Ctrl+F5`.
