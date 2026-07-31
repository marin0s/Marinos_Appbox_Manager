# V0.4.3.1 — Correctif changelog

Corrige l’erreur Jinja :

```text
TypeError: 'builtin_function_or_method' object is not iterable
```

La clé `items` du dictionnaire de release entrait en collision avec la méthode Python `dict.items`.

Correction :

```jinja2
{% for item in release['items'] %}
```

Le changelog reste limité aux versions majeures et n’affiche pas ce patch intermédiaire.

## Installation

```bash
cd /root
unzip marinos-appbox-manager-v0.4.3.1-artemis.zip
cd appbox-manager-poc-v0.4.3.1
chmod 755 upgrade-v0.4.3.1-artemis.sh
./upgrade-v0.4.3.1-artemis.sh
```
