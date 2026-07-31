# Marinos AppBox Manager v1.6.0-alpha.4

## Correctifs

- Claim Plex distribué via `agent_commands` / `appbox_action: claim`; aucun accès Docker local depuis CRONOS.
- Jeton Plex temporaire, restauré hors `compose.yml` et `.env`, puis masqué dans la file persistante.
- Réconciliation runtime priorisant le label canonique `marinos.appbox.id`.
- Références Plex compactes : exclusion de `Metadata`, `Media`, `Cache`, `Logs`, `Crash Reports` et `Codecs`; les bases SQLite assainies restent incluses.

## Limites

- Les archives déjà construites ne sont pas modifiées. Il faut créer une nouvelle version de référence pour bénéficier de la réduction.
- Le défaut visuel de double tiret dans certains noms de conteneurs n'est pas modifié dans cette livraison.
