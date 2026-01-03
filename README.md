# Health OS API

API Python qui agrège les données de santé depuis Garmin Connect et YAZIO.

## 🚀 Déploiement sur Railway

### 1. Créer un nouveau projet Railway

1. Va sur [railway.app](https://railway.app)
2. Clique sur "New Project"
3. Choisis "Deploy from GitHub repo" ou "Empty Project"

### 2. Configurer les variables d'environnement

Dans Railway, va dans "Variables" et ajoute :

```
GARMIN_EMAIL=ton-email@garmin.com
GARMIN_PASSWORD=ton-mot-de-passe-garmin
YAZIO_EMAIL=ton-email@yazio.com
YAZIO_PASSWORD=ton-mot-de-passe-yazio
```

### 3. Déployer

Si tu as connecté un repo GitHub, Railway déploie automatiquement.

Sinon, tu peux utiliser Railway CLI :
```bash
railway login
railway init
railway up
```

## 📡 Endpoints

### `GET /`
Health check - vérifie que l'API fonctionne.

### `GET /sync`
Récupère toutes les données de santé (Garmin + YAZIO).

**Paramètres** :
- `date_str` (optionnel) : Date au format `YYYY-MM-DD`. Par défaut : aujourd'hui.

**Exemple** :
```bash
curl https://ton-app.railway.app/sync
curl https://ton-app.railway.app/sync?date_str=2026-01-03
```

**Réponse** :
```json
{
  "date": "2026-01-03",
  "bodyBattery": 85,
  "sleepScore": 82,
  "sleepDuration": "7h 45",
  "weight": 78.2,
  "weightChange": -0.4,
  "calories": 1850,
  "caloriesGoal": 2000,
  "protein": 140,
  "proteinGoal": 150,
  "carbs": 210,
  "carbsGoal": 250,
  "fat": 55,
  "fatGoal": 70,
  "lastUpdated": "2026-01-03T10:30:00",
  "errors": []
}
```

### `GET /garmin`
Récupère uniquement les données Garmin (pour debug).

### `GET /yazio`
Récupère uniquement les données YAZIO (pour debug).

## 🔧 Développement local

```bash
# Créer un environnement virtuel
python -m venv venv
source venv/bin/activate  # Linux/Mac
# ou venv\Scripts\activate  # Windows

# Installer les dépendances
pip install -r requirements.txt

# Copier et configurer les variables d'environnement
cp .env.example .env
# Éditer .env avec tes credentials

# Lancer le serveur
python main.py
```

Le serveur sera disponible sur `http://localhost:8000`.

Documentation Swagger : `http://localhost:8000/docs`

## 📋 Structure N8N

Une fois déployé, crée un workflow N8N :

1. **Trigger** : Schedule (Cron) - tous les jours à 8h
2. **HTTP Request** : GET `https://ton-app.railway.app/sync`
3. **GitHub** : Push le JSON dans un repo (ou autre action)

## ⚠️ Notes importantes

- Les APIs Garmin et YAZIO sont **non-officielles** et peuvent changer.
- Les tokens Garmin sont cachés en mémoire. Si le service redémarre, il se reconnecte automatiquement.
- Garde tes credentials secrets ! Ne les commit jamais dans Git.
