# Contribuer à Marinos AppBox Manager

## Workflow Git

1. Partir de `develop` à jour.
2. Créer une branche `feature/<sujet>` ou `fix/<sujet>`.
3. Produire des commits petits et cohérents.
4. Exécuter les tests concernés.
5. Mettre à jour le `CHANGELOG.md` et la documentation utile.
6. Ouvrir une Pull Request vers `develop`.

## Qualité attendue

Une évolution n'est terminée que si le code, les tests, le changelog et la documentation sont cohérents.

Ne jamais commiter :

- bases SQLite de production ;
- `.env`, tokens, webhooks ou clés privées ;
- archives d'images de référence ;
- logs, caches ou données runtime.

## Validation minimale

```bash
python3 -m compileall app agent
python3 -m pytest -q
```

Pour une modification du Control Plane :

```bash
docker compose build appbox-manager
docker compose up -d
curl -fsS http://127.0.0.1:8090/health
```

Pour une modification de l'agent, valider au minimum la compilation Python et les tests ciblés avant de le publier depuis l'interface.
