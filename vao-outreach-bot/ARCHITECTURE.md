# VAO Outreach Bot — Architecture Complète pour Claude Code

## Vue d'ensemble

Bot d'outreach automatisé qui envoie des messages personnalisés via les formulaires de contact des sites web de paysagistes français. Le système enrichit les prospects, les score, puis exécute une séquence de 3-5 messages avec suivi CRM complet.

## Stack technique

- **Runtime** : Python 3.11+ (asyncio)
- **Browser automation** : Playwright (async API)
- **LLM** : DeepSeek API (deepseek-chat) pour interpréter les formulaires ambigus
- **Base de données** : Supabase PostgreSQL
- **Serveur** : Hetzner Ubuntu (déjà en place, IP: 178.104.104.36)
- **Proxies** : IPRoyal résidentiel FR (optionnel — scaling Phase 5)
- **Process manager** : systemd

---

## Structure du projet

```
vao-outreach-bot/
├── .env                          # Variables d'environnement
├── .env.example                  # Template des variables
├── requirements.txt              # Dépendances Python
├── README.md                     # Documentation usage
├── setup.py                      # Installation
│
├── config/
│   ├── settings.py               # Config centralisée (charge .env)
│   ├── scoring.py                # Règles de scoring ICP
│   └── sequences.py              # Définition des séquences de messages
│
├── db/
│   ├── schema.sql                # Schéma Supabase complet
│   ├── views.sql                 # Vues analytics
│   ├── seed.sql                  # Import initial des 21K prospects
│   └── client.py                 # Client Supabase (wrapper)
│
├── enrichment/
│   ├── enricher.py               # Orchestrateur d'enrichissement
│   ├── site_analyzer.py          # Visite le site, extrait les infos
│   ├── form_detector.py          # Détecte si formulaire de contact existe
│   └── scorer.py                 # Calcule le score ICP
│
├── outreach/
│   ├── campaign_runner.py        # Orchestrateur principal de campagne
│   ├── form_filler.py            # Remplit et soumet les formulaires
│   ├── field_mapper.py           # Heuristique + DeepSeek pour mapper les champs
│   ├── message_builder.py        # Génère le message personnalisé
│   └── stealth.py                # Config anti-détection Playwright
│
├── tracking/
│   ├── response_tracker.py       # Ingestion des réponses (IMAP polling) (Phase 5 — optionnel)
│   ├── call_list.py              # Génère la liste d'appels du jour
│   └── stats.py                  # Calcul des KPIs
│
├── scripts/
│   ├── import_prospects.py       # Import CSV/JSON → Supabase
│   ├── run_enrichment.py         # CLI: lance l'enrichissement sur N prospects
│   ├── run_campaign.py           # CLI: lance l'envoi du jour
│   ├── run_response_check.py     # CLI: vérifie les réponses
│   └── generate_call_list.py     # CLI: affiche les appels à passer
│
├── services/
│   ├── deepseek.py               # Client DeepSeek API
│   ├── playwright_manager.py     # Gestion du browser (lifecycle, contextes)
│   └── proxy_manager.py          # Rotation des proxies (Phase 5 — optionnel)
│
├── templates/
│   ├── sequence_1_pain.txt       # Message 1 — angle douleur admin
│   ├── sequence_2_proof.txt      # Message 2 — preuve sociale
│   ├── sequence_3_urgency.txt    # Message 3 — urgence / facture 2026
│   ├── sequence_4_email.txt      # Message 4 — relance email directe
│   └── sequence_5_breakup.txt    # Message 5 — dernier message
│
├── tests/
│   ├── test_field_mapper.py      # Tests du mapping de champs
│   ├── test_scorer.py            # Tests du scoring
│   ├── test_message_builder.py   # Tests de la personnalisation
│   └── fixtures/                 # HTML de formulaires réels pour tests
│       ├── wordpress_cf7.html
│       ├── elementor.html
│       ├── wix.html
│       └── custom.html
│
└── systemd/
    ├── vao-enrichment.service    # Service enrichissement
    ├── vao-campaign.service      # Service envoi quotidien
    ├── vao-campaign.timer        # Timer (lun/mer/ven 8h)
    └── vao-responses.service     # Service check réponses
```

---

## Schéma de base de données (Supabase PostgreSQL)

### Table `prospects`

```sql
CREATE TABLE prospects (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    
    -- Données brutes (import initial)
    company_name TEXT NOT NULL,
    owner_name TEXT,
    owner_first_name TEXT,          -- Extrait de owner_name
    phone TEXT,
    email TEXT,
    address TEXT,
    city TEXT,
    postal_code TEXT,
    department TEXT,                  -- Ex: "75", "33", "69"
    region TEXT,                      -- Ex: "Île-de-France", "Occitanie" (déduit du département, pour preuve sociale Cyrano)
    website TEXT,
    siret TEXT,
    naf_code TEXT,                    -- 8130Z = paysagiste
    forme_juridique TEXT,
    
    -- Données enrichies (par le bot)
    has_contact_form BOOLEAN DEFAULT NULL,
    contact_form_url TEXT,           -- URL exacte de la page avec le formulaire
    form_html_snapshot TEXT,         -- HTML du formulaire (pour debug)
    form_fields_mapping JSONB,      -- Mapping des champs détecté
    site_keywords TEXT[],            -- Mots-clés extraits du site
    activity_types TEXT[],           -- Ex: ['creation_jardin', 'entretien', 'elagage', 'terrasse', 'piscine']
    site_quality_score INT,          -- 0-10 : qualité du site web
    has_portfolio BOOLEAN,           -- A une page réalisations
    
    -- Scoring
    icp_score FLOAT DEFAULT 0,
    tier INT,                        -- 1 ou 2
    scoring_details JSONB,           -- Détail du scoring pour debug
    
    -- Statut campagne
    campaign_status TEXT DEFAULT 'new'
        CHECK (campaign_status IN (
            'new',                   -- Pas encore traité
            'enrichment_pending',    -- En attente d'enrichissement
            'enrichment_failed',     -- Enrichissement échoué
            'enriched',              -- Enrichi, pas encore scoré
            'scored',                -- Scoré, en attente d'envoi
            'in_sequence',           -- Séquence en cours
            'sequence_complete',     -- Toute la séquence envoyée
            'responded',             -- A répondu
            'call_scheduled',        -- Appel planifié (Tier 1)
            'called',                -- Appelé
            'demo_booked',           -- Démo bookée
            'converted',             -- Client
            'not_interested',        -- Pas intéressé
            'opted_out',             -- A demandé STOP
            'no_form',               -- Pas de formulaire de contact
            'invalid'                -- Site HS, données invalides
        )),
    current_sequence_step INT DEFAULT 0,  -- 0 = pas encore commencé, 1-5
    next_action_date DATE,                -- Prochaine action à faire
    next_action_type TEXT,                -- 'send_form', 'send_email', 'call'
    
    -- Métadonnées
    enriched_at TIMESTAMPTZ,
    scored_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Index pour les queries fréquentes
CREATE INDEX idx_prospects_status ON prospects(campaign_status);
CREATE INDEX idx_prospects_next_action ON prospects(next_action_date, next_action_type) 
    WHERE campaign_status IN ('scored', 'in_sequence', 'call_scheduled');
CREATE INDEX idx_prospects_tier ON prospects(tier) WHERE tier IS NOT NULL;
CREATE INDEX idx_prospects_score ON prospects(icp_score DESC);
```

### Table `submissions`

```sql
CREATE TABLE submissions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    prospect_id UUID NOT NULL REFERENCES prospects(id),
    
    -- Séquence
    sequence_step INT NOT NULL,       -- 1, 2, 3, 4, 5
    message_variant TEXT,             -- 'A', 'B', 'C' (pour A/B test)
    channel TEXT NOT NULL             -- 'contact_form' ou 'email'
        CHECK (channel IN ('contact_form', 'email')),
    
    -- Contenu envoyé
    message_sent TEXT NOT NULL,       -- Le message exact envoyé
    subject_sent TEXT,                -- Objet (si formulaire a un champ sujet)
    sender_name TEXT,                 -- Nom utilisé dans le formulaire
    sender_email TEXT,                -- Email utilisé dans le formulaire
    
    -- Résultat technique
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN (
            'pending',
            'in_progress',
            'success',               -- Formulaire soumis avec succès
            'failed_no_form',        -- Pas de formulaire trouvé
            'failed_captcha',        -- CAPTCHA bloquant
            'failed_mapping',        -- Impossible de mapper les champs
            'failed_submit',         -- Erreur à la soumission
            'failed_timeout',        -- Timeout
            'failed_blocked',        -- IP bloquée
            'failed_other',          -- Autre erreur
            'skipped'                -- Skippé (opt-out, déjà répondu, etc.)
        )),
    error_details TEXT,
    
    -- Technique
    proxy_ip TEXT,
    page_load_time_ms INT,
    form_fill_time_ms INT,
    screenshot_path TEXT,             -- Screenshot post-soumission (debug)
    
    -- Timestamps
    attempted_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX idx_submissions_prospect ON submissions(prospect_id);
CREATE INDEX idx_submissions_status ON submissions(status);
CREATE INDEX idx_submissions_step ON submissions(sequence_step, message_variant);
```

### Table `responses`

```sql
CREATE TABLE responses (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    prospect_id UUID REFERENCES prospects(id),
    submission_id UUID REFERENCES submissions(id),
    
    -- Contenu
    response_channel TEXT NOT NULL    -- 'email', 'phone', 'form'
        CHECK (response_channel IN ('email', 'phone', 'form', 'other')),
    response_body TEXT,               -- Contenu de la réponse
    response_subject TEXT,
    
    -- Classification
    sentiment TEXT                    -- 'positive', 'neutral', 'negative', 'opt_out'
        CHECK (sentiment IN ('positive', 'neutral', 'negative', 'opt_out')),
    intent TEXT                       -- 'demo_request', 'question', 'not_interested', 'stop', 'other'
        CHECK (intent IN ('demo_request', 'question', 'not_interested', 'stop', 'other')),
    notes TEXT,                       -- Notes manuelles de Quentin
    
    -- Timestamps
    received_at TIMESTAMPTZ DEFAULT now(),
    created_at TIMESTAMPTZ DEFAULT now()
);
```

### Table `call_log`

```sql
CREATE TABLE call_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    prospect_id UUID NOT NULL REFERENCES prospects(id),
    
    called_at TIMESTAMPTZ DEFAULT now(),
    duration_seconds INT,
    outcome TEXT NOT NULL
        CHECK (outcome IN (
            'answered_interested',
            'answered_not_interested',
            'answered_callback',
            'voicemail',
            'no_answer',
            'wrong_number',
            'demo_booked'
        )),
    notes TEXT,
    next_action TEXT,                 -- Action à planifier suite à l'appel
    next_action_date DATE,
    
    created_at TIMESTAMPTZ DEFAULT now()
);
```

### Table `campaign_config`

```sql
CREATE TABLE campaign_config (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT now()
);

-- Config initiale
INSERT INTO campaign_config (key, value) VALUES
('daily_send_limit', '50'),
('delay_between_sends_min', '60'),
('delay_between_sends_max', '120'),
('days_between_steps', '4'),
('days_before_call', '2'),
('send_days', '["monday", "wednesday", "friday"]'),
('send_hour_start', '8'),
('send_hour_end', '10'),
('active_variants', '["A", "B"]'),
('sender_name', '"Quentin"'),
('sender_email', '"devis@vao-solution.com"'),
('sender_phone', '"06XXXXXXXX"');
```

### Vues analytics

```sql
-- Performance par variant
CREATE VIEW v_variant_performance AS
SELECT 
    s.message_variant,
    s.sequence_step,
    COUNT(*) as total_sent,
    COUNT(*) FILTER (WHERE s.status = 'success') as successful,
    COUNT(r.id) as responses,
    COUNT(r.id) FILTER (WHERE r.sentiment = 'positive') as positive_responses,
    COUNT(r.id) FILTER (WHERE r.intent = 'demo_request') as demo_requests,
    ROUND(COUNT(r.id)::numeric / NULLIF(COUNT(*) FILTER (WHERE s.status = 'success'), 0) * 100, 2) as response_rate
FROM submissions s
LEFT JOIN responses r ON r.submission_id = s.id
GROUP BY s.message_variant, s.sequence_step
ORDER BY s.sequence_step, s.message_variant;

-- Stats quotidiennes
CREATE VIEW v_daily_stats AS
SELECT 
    DATE(s.attempted_at) as day,
    COUNT(*) as attempts,
    COUNT(*) FILTER (WHERE s.status = 'success') as successes,
    COUNT(*) FILTER (WHERE s.status LIKE 'failed%') as failures,
    ROUND(COUNT(*) FILTER (WHERE s.status = 'success')::numeric / NULLIF(COUNT(*), 0) * 100, 1) as success_rate,
    COUNT(DISTINCT s.proxy_ip) as proxies_used
FROM submissions s
WHERE s.attempted_at IS NOT NULL
GROUP BY DATE(s.attempted_at)
ORDER BY day DESC;

-- Liste d'appels du jour
CREATE VIEW v_call_list_today AS
SELECT 
    p.id,
    p.owner_first_name,
    p.owner_name,
    p.company_name,
    p.phone,
    p.city,
    p.website,
    p.icp_score,
    p.activity_types,
    s.message_sent as last_message,
    s.completed_at as last_sent_at
FROM prospects p
JOIN submissions s ON s.prospect_id = p.id AND s.status = 'success'
WHERE p.tier = 1
  AND p.next_action_type = 'call'
  AND p.next_action_date <= CURRENT_DATE
  AND p.campaign_status NOT IN ('opted_out', 'not_interested', 'demo_booked', 'converted')
ORDER BY p.icp_score DESC;

-- Pipeline overview
CREATE VIEW v_pipeline AS
SELECT 
    campaign_status,
    tier,
    COUNT(*) as count,
    AVG(icp_score) as avg_score
FROM prospects
WHERE campaign_status != 'new'
GROUP BY campaign_status, tier
ORDER BY tier, campaign_status;
```

---

## Configuration

### `.env.example`

```env
# Supabase
SUPABASE_URL=https://xxxxx.supabase.co
SUPABASE_SERVICE_KEY=eyJ...
SUPABASE_ANON_KEY=eyJ...

# DeepSeek
DEEPSEEK_API_KEY=sk-...
DEEPSEEK_MODEL=deepseek-chat
DEEPSEEK_BASE_URL=https://api.deepseek.com

# Resend (emails directs steps 4-5, from visible sur le screenshot)
RESEND_API_KEY=re_...
RESEND_FROM=devis@vao-solution.com

# Campagne
DAILY_SEND_LIMIT=50
SEND_DAYS=monday,wednesday,friday
SEND_HOUR_START=8
SEND_HOUR_END=10

# ─── OPTIONNEL — Phase 5 scaling ──────────────────────────────────────────
# Activer les proxies uniquement si le taux d'échec dépasse 20% à cause de
# blocages IP. En Phase 1-4, l'IP Hetzner directe suffit.
# PROXY_HOST=geo.iproyal.com
# PROXY_PORT=12321
# PROXY_USER=xxx
# PROXY_PASS=xxx
# PROXY_COUNTRY=fr

# ─── OPTIONNEL — Phase 5 scaling ──────────────────────────────────────────
# Activer le polling IMAP uniquement quand le volume justifie l'automatisation
# du suivi des réponses. En Phase 1-4, Quentin checke manuellement
# devis@vao-solution.com et logge les réponses/opt-outs dans Supabase à la main.
# IMAP_HOST=imap.zohomail.eu
# IMAP_USER=replies@vao-solution.com
# IMAP_PASS=xxx
```

### `config/scoring.py`

```python
"""
Règles de scoring ICP pour les paysagistes.
Score total = somme des points. Max théorique ≈ 12.
Tier 1 : score >= 5
Tier 2 : score 2-4
Exclu  : score < 2
"""

SCORING_RULES = {
    # --- Qualité du site web ---
    "has_professional_site": {
        "points": 2.0,
        "description": "Site web professionnel (pas juste une page jaune)",
        "check": "site_quality_score >= 5"
    },
    "has_contact_form": {
        "points": 1.0,
        "description": "Formulaire de contact fonctionnel détecté",
        "check": "has_contact_form == True"
    },
    "has_portfolio": {
        "points": 1.0,
        "description": "Page réalisations/portfolio présente",
        "check": "has_portfolio == True"
    },
    
    # --- Type d'activité ---
    "high_margin_services": {
        "points": 2.0,
        "description": "Services à forte marge (création, aménagement, piscine, terrasse)",
        "check": "any(a in activity_types for a in ['creation_jardin', 'amenagement', 'piscine', 'terrasse'])"
    },
    "medium_margin_services": {
        "points": 1.0,
        "description": "Services marge moyenne (entretien régulier, élagage)",
        "check": "any(a in activity_types for a in ['entretien', 'elagage', 'taille'])"
    },
    "low_value_only": {
        "points": 0,
        "description": "Uniquement petits travaux / tonte",
        "check": "activity_types == ['tonte'] or activity_types == ['petit_entretien']"
    },
    
    # --- Structure ---
    "independent_or_small": {
        "points": 1.0,
        "description": "Indépendant ou petite structure (< 5 salariés)",
        "check": "forme_juridique in ['EI', 'EIRL', 'EURL', 'SASU', 'auto-entrepreneur'] or not forme_juridique"
    },
    "architect_paysagiste": {
        "points": 0.5,
        "description": "Architecte paysagiste (moins ICP mais potentiel)",
        "check": "'architecte' in site_keywords"
    },
    "bureau_etudes": {
        "points": -1.0,
        "description": "Bureau d'études (pas ICP)",
        "check": "'bureau_etudes' in activity_types"
    },
    
    # --- Localisation ---
    "zone_urbaine": {
        "points": 1.0,
        "description": "Zone urbaine / périurbaine (plus de clients, plus de devis)",
        "check": "department in URBAN_DEPARTMENTS"
    },
    
    # --- Contact ---
    "has_phone": {
        "points": 0.5,
        "description": "Numéro de téléphone disponible",
        "check": "phone is not None"
    },
    "has_email": {
        "points": 0.5,
        "description": "Email disponible (pour relances steps 4-5)",
        "check": "email is not None"
    },
}

URBAN_DEPARTMENTS = [
    "75", "92", "93", "94", "78", "91", "95", "77",  # Île-de-France
    "69", "13", "31", "33", "59", "44", "67", "06",   # Grandes métropoles
    "34", "35", "38", "42", "54", "57", "76",          # Villes moyennes+
]

TIER_THRESHOLDS = {
    "tier_1": 5.0,   # Score >= 5 → mail + appel
    "tier_2": 2.0,   # Score 2-4 → mail uniquement
    "exclude": 2.0,  # Score < 2 → pas de campagne
}
```

### `config/sequences.py`

```python
"""
Définition des séquences de messages.
Chaque step a un délai, un canal, et des variants A/B.
"""

SEQUENCE = [
    {
        "step": 1,
        "channel": "contact_form",
        "delay_days_after_previous": 0,  # Premier envoi immédiat
        "template": "sequence_1_pain.txt",
        "variants": ["A", "B"],
        "call_after_days": 2,  # Tier 1 uniquement
    },
    {
        "step": 2,
        "channel": "contact_form",
        "delay_days_after_previous": 4,
        "template": "sequence_2_proof.txt",
        "variants": ["A", "B"],
        "call_after_days": 2,
    },
    {
        "step": 3,
        "channel": "contact_form",
        "delay_days_after_previous": 5,
        "template": "sequence_3_urgency.txt",
        "variants": ["A", "B"],
        "call_after_days": 2,
    },
    {
        "step": 4,
        "channel": "email",  # Bascule sur email direct via Resend
        "delay_days_after_previous": 7,
        "template": "sequence_4_email.txt",
        "variants": ["A"],
        "call_after_days": None,  # Pas d'appel
        "email_subject": "devis paysagiste {ville}",  # Cyrano: minuscules, 4 mots max, même objet steps 4-5
    },
    {
        "step": 5,
        "channel": "email",
        "delay_days_after_previous": 5,
        "template": "sequence_5_breakup.txt",
        "variants": ["A"],
        "call_after_days": None,
        "email_subject": "devis paysagiste {ville}",  # Même objet que step 4 (Cyrano: garder le fil)
    },
]
```

---

## Modules principaux — Spécifications

### 1. `enrichment/site_analyzer.py`

**Rôle** : Visite le site web d'un prospect avec Playwright headless, extrait les informations clés.

**Input** : `prospect.website` (URL)

**Output** : dict avec :
- `has_contact_form` : bool
- `contact_form_url` : URL de la page contenant le formulaire
- `form_html` : innerHTML du `<form>` détecté
- `form_fields_mapping` : résultat du field_mapper (ou None si pas de form)
- `site_keywords` : liste de mots-clés extraits (h1, h2, meta description, title)
- `activity_types` : liste classifiée parmi un vocabulaire contrôlé
- `site_quality_score` : 0-10 basé sur : HTTPS, responsive, vitesse, contenu, images
- `has_portfolio` : bool (détecte pages /realisations, /portfolio, /galerie, /projets)

**Logique** :
1. Ouvrir la page d'accueil, bloquer images/fonts/CSS (économie bande passante)
2. Extraire title, meta description, h1, h2, texte visible des paragraphes
3. Classifier activity_types via matching de mots-clés français :
   - `creation_jardin` : "création", "conception", "aménagement paysager"
   - `entretien` : "entretien", "tonte", "taille de haies"
   - `elagage` : "élagage", "abattage", "soin des arbres"
   - `terrasse` : "terrasse", "dallage", "pavage"
   - `piscine` : "piscine", "bassin"
   - `cloture` : "clôture", "portail", "grillage"
   - `arrosage` : "arrosage automatique", "irrigation"
   - `architecte` : "architecte paysagiste", "bureau d'études"
4. Chercher le formulaire de contact :
   - D'abord sur la page d'accueil
   - Puis naviguer vers /contact, /nous-contacter, /contactez-nous
   - Chercher les liens contenant "contact" dans le texte ou href
5. Si formulaire trouvé → lancer `form_detector.py` et `field_mapper.py`
6. Évaluer `site_quality_score` (présence HTTPS, design moderne, contenu riche, etc.)

**Timeout** : 15s par page, 30s total par prospect. Skip si timeout.

### 2. `outreach/field_mapper.py`

**Rôle** : Identifie quel champ du formulaire correspond à quoi (nom, email, téléphone, message, sujet).

**Approche hybride** (heuristique d'abord, LLM si ambigu) :

**Phase 1 — Heuristique (gratuit, résout ~75% des cas)** :
```python
FIELD_PATTERNS = {
    "name": {
        "input_types": ["text"],
        "name_patterns": ["nom", "name", "prenom", "firstname", "lastname", "your-name"],
        "placeholder_patterns": ["votre nom", "nom", "prénom", "name"],
        "label_patterns": ["nom", "prénom", "name"],
    },
    "email": {
        "input_types": ["email"],
        "name_patterns": ["email", "mail", "courriel", "your-email"],
        "placeholder_patterns": ["email", "votre email", "adresse email"],
        "label_patterns": ["email", "e-mail", "courriel"],
    },
    "phone": {
        "input_types": ["tel"],
        "name_patterns": ["tel", "phone", "telephone", "mobile"],
        "placeholder_patterns": ["téléphone", "phone", "06", "07"],
        "label_patterns": ["téléphone", "phone", "portable"],
    },
    "subject": {
        "input_types": ["text"],
        "name_patterns": ["subject", "sujet", "objet", "your-subject"],
        "placeholder_patterns": ["sujet", "objet", "subject"],
        "label_patterns": ["sujet", "objet", "subject"],
    },
    "message": {
        "input_types": [],  # textarea
        "element": "textarea",
        "name_patterns": ["message", "content", "body", "your-message", "commentaire"],
        "placeholder_patterns": ["message", "votre message", "écrivez"],
        "label_patterns": ["message", "votre message", "commentaire"],
    },
}
```

**Phase 2 — DeepSeek (si heuristique < 80% confiance)** :

System prompt pour DeepSeek :
```
Tu es un expert en analyse de formulaires HTML.
Analyse le formulaire HTML suivant et retourne un JSON avec le mapping des champs.

Pour chaque champ du formulaire (<input>, <textarea>, <select>), identifie sa fonction parmi :
- "name" : champ nom/prénom
- "email" : champ email
- "phone" : champ téléphone
- "subject" : champ sujet/objet
- "message" : champ message principal (textarea)
- "company" : champ nom d'entreprise
- "honeypot" : champ caché (ne PAS remplir)
- "other" : champ non pertinent

Retourne UNIQUEMENT un JSON valide, sans aucun texte avant ou après :
{
  "fields": [
    {"selector": "CSS selector du champ", "role": "name|email|phone|subject|message|company|honeypot|other", "required": true|false}
  ],
  "submit_selector": "CSS selector du bouton submit",
  "form_selector": "CSS selector du form",
  "confidence": 0.0-1.0
}
```

**Détection honeypot — CRITIQUE** :
Avant tout remplissage, vérifier pour CHAQUE champ :
- `display: none` ou `visibility: hidden` dans le style computed
- `opacity: 0`
- `position: absolute` avec `left: -9999px`
- `tabindex: -1`
- `aria-hidden: true`
- `type: hidden`
- Nom contenant "honeypot", "hp", "website", "url", "fax"
- Champ avec `autocomplete="off"` ET caché

→ Si un de ces signaux : marquer comme `honeypot`, NE PAS remplir.

### 3. `outreach/form_filler.py`

**Rôle** : Prend un prospect enrichi + un message, ouvre la page du formulaire, remplit et soumet.

**Flow** :
1. Créer un contexte Playwright avec stealth config
2. Naviguer vers `prospect.contact_form_url`
3. Attendre le formulaire (`page.wait_for_selector('form', timeout=10000)`)
4. Charger le `form_fields_mapping` du prospect
5. Pour chaque champ à remplir :
   - Scroll jusqu'au champ (`element.scroll_into_view_if_needed()`)
   - Cliquer sur le champ
   - Attendre 200-500ms (random)
   - Taper le texte avec `press_sequentially(text, delay=random(30, 100))`
   - Attendre 300-800ms avant le champ suivant
6. Simuler un scroll léger (humain qui relit)
7. Attendre 2-5s (humain qui relit le formulaire)
8. Cliquer sur submit
9. Attendre la réponse :
   - Écouter un changement de page (redirect)
   - OU détecter un message de succès dans la page (merci, envoyé, thank you, etc.)
   - OU écouter la réponse réseau POST
10. Screenshot de confirmation
11. Log le résultat dans Supabase

**Gestion d'erreurs** :
- CAPTCHA détecté → status `failed_captcha`, skip (pas de solve au début)
- Timeout > 15s → status `failed_timeout`
- Champ required non mappé → status `failed_mapping`
- Erreur réseau → retry 1 fois après 30s, puis `failed_other`
- Alerte/confirm dialog → dismiss automatiquement

### 4. `outreach/message_builder.py`

**Rôle** : Génère le message personnalisé pour un prospect donné, à une étape donnée. DOIT respecter les principes Cyrano (voir section "Principes de copywriting — Méthode Cyrano").

**Variables de personnalisation disponibles** :
```python
{
    "prenom": "Pierre",                    # Prénom du gérant
    "nom_entreprise": "Jardins du Sud",
    "ville": "Montpellier",
    "department": "34",                     # Pour régionaliser la preuve sociale
    "region": "Hérault",                    # Déduit du département
    "activite_principale": "création de jardins",  # Déduit de activity_types[0]
    "site_web": "www.jardins-du-sud.fr",
}
```

**Règles de génération (Cyrano-compliant)** :
- Structure APPC : Accroche personnalisée → Problème concret → Proposition valeur → CTA simple
- Maximum 80 mots pour les messages formulaire (steps 1-3)
- Maximum 120 mots pour les emails (steps 4-5)
- Vouvoiement systématique, mais prénom en accroche ("Bonjour Pierre,")
- Pas de "Cher", "Madame/Monsieur", "J'espère que vous allez bien"
- Pas de jargon tech, pas d'anglicisme, pas de mot "SaaS" ou "CRM"
- 1 message = 1 problème = 1 question (JAMAIS de liste de features)
- CTA = yes/no question ou action concrète (JAMAIS "n'hésitez pas" ou "à votre disposition")
- Maximum 1 lien par message
- Chaque message DOIT contenir : identification expéditeur (Quentin — VAO) + numéro de téléphone
- Steps 4-5 : objet email tout en minuscules, 4 mots max, même objet pour les 2 steps

**Validation post-génération (checklist anti-erreurs Cyrano)** :
Le message_builder doit vérifier avant d'envoyer que le message :
1. Ne contient PAS de prix ou tarif
2. Ne demande PAS de RDV (steps 1-3)
3. Ne commence PAS par parler de VAO / de l'expéditeur
4. Ne contient PAS de "J'espère que vous allez bien" ou small talk
5. Ne contient PAS plus d'1 lien
6. Ne contient PAS de liste de features/services
7. Contient bien le {prenom} ou {nom_entreprise} (personnalisation visible)
8. Se termine par une question concrète (pas un CTA passif)
9. Fait moins de 80 mots (formulaire) ou 120 mots (email)

### 5. `outreach/campaign_runner.py`

**Rôle** : Orchestrateur principal. Exécuté via `scripts/run_campaign.py` ou via le timer systemd.

**Flow quotidien** :
```
1. Vérifier si c'est un jour d'envoi (lun/mer/ven)
2. Charger la config (daily_send_limit, etc.)
3. Vérifier les opt-outs reçus → mettre à jour les statuts
   NOTE : En Phase 1-4, cette étape est manuelle. Quentin checke
   devis@vao-solution.com et met à jour les opt-outs directement dans
   Supabase (campaign_status = 'opted_out'). Le campaign_runner respecte
   le statut opt_out en DB quel que soit son origine (manuelle ou IMAP).
   Le response_tracker IMAP (Phase 5) automatise cette vérification.
4. Requêter les prospects éligibles :
   - campaign_status IN ('scored', 'in_sequence')
   - next_action_date <= today
   - next_action_type = 'send_form' ou 'send_email'
   - campaign_status != 'opted_out'
   - Triés par icp_score DESC
   - LIMIT daily_send_limit
5. Pour chaque prospect :
   a. Déterminer le step et le variant
   b. Construire le message personnalisé
   c. Si channel = contact_form → form_filler.py
   d. Si channel = email → Resend API
   e. Logger dans submissions
   f. Mettre à jour prospect :
      - current_sequence_step += 1
      - next_action_date = today + delay_days
      - Si Tier 1 : planifier l'appel (next_action_type = 'call', next_action_date = today + 2)
6. Générer le rapport du jour (succès/échecs/stats)
7. Générer la liste d'appels pour dans 2 jours
```

### 6. `outreach/stealth.py`

**Rôle** : Configuration anti-détection pour Playwright.

**Note proxy** : Le proxy est optionnel. Si aucun proxy n'est configuré dans `.env`, le bot utilise directement l'IP du serveur Hetzner. La rotation de proxy ne devient nécessaire que si le taux d'échec `failed_blocked` dépasse ~20%, signe que certains sites bloquent l'IP Hetzner. En Phase 1-4, l'IP directe suffit largement pour les volumes visés (< 50/jour).

```python
import random

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

VIEWPORTS = [
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1920, "height": 1080},
]

def get_stealth_context_options():
    """Retourne les options pour browser.new_context() avec anti-détection."""
    return {
        "user_agent": random.choice(USER_AGENTS),
        "viewport": random.choice(VIEWPORTS),
        "locale": "fr-FR",
        "timezone_id": "Europe/Paris",
        "geolocation": None,
        "permissions": [],
        "java_script_enabled": True,
        "bypass_csp": False,
        "ignore_https_errors": True,
    }

# Délais humains (en ms)
DELAYS = {
    "between_keystrokes": (30, 100),     # Vitesse de frappe
    "between_fields": (300, 800),         # Pause entre champs
    "before_submit": (2000, 5000),        # Pause avant envoi (relecture)
    "after_page_load": (1000, 3000),      # Pause après chargement
    "between_prospects": (60000, 120000), # 1-2 min entre chaque prospect
}
```

### 7. `tracking/response_tracker.py`

**Rôle** : Poll la boîte mail IMAP toutes les 30 min, matche les réponses aux prospects.

> **Phase 5 — optionnel.** Ce module automatise le suivi des réponses par IMAP.
> En Phase 1-4, Quentin checke manuellement les réponses sur devis@vao-solution.com
> et les logge à la main dans Supabase (table `responses` + mise à jour
> `campaign_status` → `responded` ou `opted_out`). Le campaign_runner fonctionne
> sans ce module : il se base uniquement sur le champ `campaign_status` en DB,
> qu'il soit mis à jour manuellement ou automatiquement.

**Matching** :
- Par email de l'expéditeur → matcher avec `prospects.email`
- Par contenu du message → chercher le nom d'entreprise ou la ville
- Par sujet → si contient "Re:" + notre sujet original

**Actions automatiques** :
- Réponse contient "stop", "désabonner", "plus de message" → `opted_out`
- Réponse positive détectée → notification (webhook Discord ou email à Quentin)
- Toute réponse → `campaign_status = 'responded'`

### 8. `tracking/call_list.py`

**Rôle** : Génère la liste d'appels du jour pour Quentin.

**Output** : Liste triée par score avec pour chaque prospect :
- Nom, prénom, entreprise
- Téléphone
- Ville
- Score ICP
- Types d'activité
- Le dernier message envoyé (pour contexte d'appel)
- Date d'envoi du dernier message

**Format** : Markdown dans le terminal + sauvegarde dans un fichier CSV.

---

## Principes de copywriting — Méthode Cyrano

Ces principes sont issus des posts de Cyrano (agence spécialisée cold email B2B, fondée par des anciens du même master que Quentin). Ils DOIVENT être respectés dans TOUS les messages générés par le bot.

### Règles absolues

**1 message = 1 problème = 1 question.**
Jamais de catalogue de services, jamais de liste de features. Chaque step de la séquence attaque UN seul angle. Si on n'a rien de nouveau à dire, on n'envoie pas. (Cyrano Posts 3, 6, 12)

**Le message parle du PROSPECT, pas de nous.**
Un message de prospection qui marche parle du prospect, pas de l'expéditeur. On ne commence JAMAIS par "On est VAO, on fait ci ça". On commence par LEUR réalité, LEUR douleur. (Cyrano Posts 10, 12, 16)

**Chaque relance apporte un nouvel angle.**
JAMAIS de "je vous relance pour savoir si vous avez vu mon premier message". Chaque step doit justifier pourquoi on recontacte : nouveau problème, preuve sociale, ressource utile, cas client. Si on n'a pas de nouvel angle, on n'envoie pas. (Cyrano Post 3)

**CTA concret, pas introspectif.**
Pour des artisans toujours dans le rush, les CTA qui marchent demandent une action concrète immédiate. Les CTA qui flop demandent une réflexion ou une introspection. (Cyrano Post 11)

Exemples de CTA qui marchent pour les paysagistes :
- "Je peux vous montrer en 2 min ?"
- "Vous voulez tester gratuitement ?"
- "Je vous envoie un accès ?"

Exemples de CTA qui NE marchent PAS :
- "Comment gérez-vous vos devis aujourd'hui ?"
- "Est-ce un sujet pour vous en ce moment ?"
- "Seriez-vous disponible pour un échange de 15 minutes ?"

**Max 3 tentatives via formulaire sans réponse.**
Au-delà de 3, le prospect a vu nos messages et s'en fout OU notre canal ne marche pas. Steps 4-5 basculent sur email direct, c'est un canal différent donc c'est acceptable. (Cyrano Post 3)

### Format et style

**Objet email (steps 4-5 via Resend) :**
- Tout en minuscules, sans exception
- 4 mots maximum
- Doit piquer la curiosité sans rien résoudre
- Personnaliser juste ce qu'il faut : prénom ou nom de boîte si pertinent
- Garder le même objet sur toute la séquence email (pour que le prospect remonte le fil)
- Exemples : "question rapide", "idée pour {nom_entreprise}", "devis en 5 min"
- INTERDITS : "Boostez vos ventes", "Découvrez notre solution", tout ce qui sent le marketing
(Cyrano Post 5)

**Pas de HTML, pas de mise en forme, pas de liens multiples :**
- Messages formulaire = texte brut par nature (OK)
- Emails Resend (steps 4-5) = plain text UNIQUEMENT, pas de HTML
- Maximum 1 lien par message (le lien d'essai gratuit)
- Pas de gras, pas d'italique, pas de couleurs, pas d'images
- Chaque lien supplémentaire est un signal spam qui nuit à la délivrabilité
(Cyrano Posts 7, 10)

**Preuve sociale de proximité :**
Quand on utilise la preuve sociale, on localise. Pas "500 paysagistes en France" mais "des paysagistes de {département/région} utilisent déjà VAO". Le concept "on est déjà chez vos voisins" rassure et crédibilise. (Cyrano Post 16)

**Vouvoiement + prénom :**
Toujours vouvoyer au premier contact (cible artisans, pas startup). Mais utiliser le prénom pour la chaleur : "Bonjour Pierre," — pas "Bonjour Monsieur Dupont,". (Cyrano Post 7 — "Version Cyrano")

**Longueur :**
- Messages formulaire (steps 1-3) : 60-90 mots max. Le prospect est sur mobile entre deux chantiers.
- Emails (steps 4-5) : 80-120 mots max. Plus court = plus lu.

### Structure de chaque message (méthode APPC Cyrano)

Chaque message suit le framework APPC issu des analyses de Cyrano (Posts 6, 7, 8, 16) :

**A** — Accroche personnalisée : Signal spécifique au prospect (ville, activité détectée, un détail de leur site). Montre qu'on a passé 30 secondes sur eux. Pas de "J'espère que vous allez bien".

**P** — Problème concret : UN seul problème que le prospect vit au quotidien. Formulé de son point de vue, pas du nôtre.

**P** — Proposition de valeur claire : Ce qu'on fait, en UNE phrase. Pas de liste, pas de catalogue. Le bénéfice, pas la feature.

**C** — CTA simple : Une yes/no question ou une action immédiate. Friction minimale. On prend le prospect par la main.

### Erreurs fatales à ne JAMAIS commettre

(Checklist pour `message_builder.py` — le bot doit vérifier que le message généré n'enfreint aucune de ces règles)

1. Parler de prix dans le premier message (Cyrano Post 6)
2. Demander un RDV au premier contact — on offre de la valeur d'abord (Cyrano Posts 6, 15)
3. Rappeler un contact précédent sans succès comme accroche — c'est un aveu d'échec (Cyrano Post 16)
4. Utiliser du small talk : "J'espère que vous allez bien" = gaspillage d'attention (Cyrano Post 16)
5. Name-dropper des clients que le prospect ne connaît pas (Cyrano Post 10)
6. Envoyer un message générique sans personnalisation visible (Cyrano Posts 6, 7, 10)
7. CTA passif : "Je reste à votre disposition" / "N'hésitez pas" — le prospect ne rappellera JAMAIS (Cyrano Post 16)
8. Lister plusieurs services ou features dans un seul message (Cyrano Post 6)
9. Mettre plus d'1 lien dans un message (Cyrano Post 10)

---

## Templates de messages

Les templates suivent la méthode APPC Cyrano. Chaque step a un angle unique. Les steps 4-5 incluent un objet email (tout en minuscules, 4 mots max).

### `templates/sequence_1_pain.txt` — Angle : douleur admin / devis le soir

**Variant A :**
```
Bonjour {prenom},

[A] En regardant votre site, j'ai vu que {nom_entreprise} faisait de la {activite_principale} à {ville}.

[P] Je suppose que comme la plupart des paysagistes indépendants, vos devis se font le soir après le chantier. C'est le retour qu'on a de tous les pros du secteur.

[P] On a créé une appli qui permet de faire un devis pro en 5 min directement depuis le terrain, sur votre téléphone.

[C] Je peux vous envoyer un accès gratuit pour tester ?

Quentin — VAO · 06XXXXXXXX
```

**Variant B :**
```
Bonjour {prenom},

[A] Je suis tombé sur les réalisations de {nom_entreprise} à {ville} — beau travail.

[P] Question rapide : vous passez combien de temps par semaine sur vos devis et factures ? Les paysagistes qu'on accompagne nous disaient 4 à 5 heures, souvent le soir.

[P] VAO permet de créer un devis depuis votre téléphone en 5 minutes, entre deux chantiers.

[C] Vous voulez tester gratuitement 14 jours ?

Quentin — VAO · 06XXXXXXXX
```

**Note message_builder :** Les marqueurs [A][P][P][C] ne sont PAS inclus dans le message final. Ils sont ici pour documenter la structure. Le message envoyé commence directement par "Bonjour".

### `templates/sequence_2_proof.txt` — Angle : preuve sociale de proximité

Nouvel angle par rapport au step 1 : on ne parle plus de la douleur, on parle des AUTRES paysagistes qui ont résolu le problème.

**Variant A :**
```
Bonjour {prenom},

[A] Un message rapide pour {nom_entreprise}.

[P] Des paysagistes de {region} qui avaient le même problème de devis chronophages ont essayé notre outil.

[P] Le retour le plus fréquent : ils ont divisé par 5 le temps passé sur leurs devis. Certains le font maintenant directement chez le client, pendant la visite.

[C] Je peux vous montrer comment ça marche en 2 min ?

Quentin — VAO · 06XXXXXXXX
```

**Variant B :**
```
Bonjour {prenom},

[A] Je reviens vers vous à propos de la gestion de devis chez {nom_entreprise}.

[P] Un paysagiste de {department} m'a dit récemment : "Avant, je passais mes dimanches soirs sur les devis. Maintenant je les fais sur le chantier en 5 min."

[P] C'est exactement ce que permet VAO — devis pro depuis le téléphone, envoi direct au client.

[C] Vous voulez essayer gratuitement ?

Quentin — VAO · 06XXXXXXXX
```

### `templates/sequence_3_urgency.txt` — Angle : conformité facturation 2026

Nouvel angle par rapport aux steps 1-2 : on ne parle plus du temps, on parle d'une obligation réglementaire.

**Variant A :**
```
Bonjour {prenom},

[A] Dernier message pour {nom_entreprise}, promis.

[P] La facturation électronique devient obligatoire en 2026 pour toutes les entreprises. Beaucoup de paysagistes indépendants ne sont pas encore équipés.

[P] VAO permet déjà de créer des devis ET des factures conformes, directement depuis le téléphone. Autant s'y mettre maintenant plutôt que de courir au dernier moment.

[C] Je vous envoie un accès gratuit 14 jours ?

Quentin — VAO · 06XXXXXXXX
```

**Variant B :**
```
Bonjour {prenom},

[A] Dernier point pour {nom_entreprise}.

[P] Avec l'obligation de facturation électronique qui arrive, les paysagistes qui n'ont pas encore d'outil de devis/facturation vont devoir s'équiper rapidement.

[P] VAO a été conçu spécifiquement pour les paysagistes : devis en 5 min, factures conformes, tout depuis le téléphone.

[C] Vous voulez tester avant que ça devienne urgent ?

Quentin — VAO · 06XXXXXXXX
```

### `templates/sequence_4_email.txt` — Angle : ressource gratuite / lead magnet

Canal : email direct via Resend (devis@vao-solution.com). Nouvel angle : on apporte de la VALEUR sans rien demander. Principe Cyrano Post 15 : "D'abord la valeur, ensuite le RDV."

**Objet email :** `devis paysagiste {ville}` (tout en minuscules, même objet pour step 4 et 5)

**Variant A :**
```
Bonjour {prenom},

Je vous avais contacté via votre site il y a quelques semaines à propos de la gestion de devis.

J'ai préparé un modèle de devis spécifique pour les paysagistes qui font de la {activite_principale}. C'est le format le plus utilisé par nos utilisateurs, avec les postes de dépense déjà pré-remplis.

Je peux vous l'envoyer ? C'est gratuit et sans engagement.

Quentin — VAO
06XXXXXXXX
```

### `templates/sequence_5_breakup.txt` — Angle : clôture élégante

Canal : email direct via Resend. Dernier message de la séquence. Méthode Cyrano Post 8 (mail de clôture) : on arrête avec élégance, on laisse la porte ouverte, on respecte le prospect.

**Objet email :** `devis paysagiste {ville}` (même objet que step 4, pour garder le fil)

**Variant A :**
```
Bonjour {prenom},

Je ne vais pas vous embêter plus longtemps.

Si la gestion de devis n'est pas un sujet pour {nom_entreprise} en ce moment, aucun souci.

Si un jour ça le devient, VAO sera toujours là : https://app.vao-solution.com

Bonne continuation et bonne saison.

Quentin — VAO
06XXXXXXXX
```

**Note :** Pas de relance après ce message, jamais. On reste humain et professionnel.

---

## Services systemd

### `systemd/vao-campaign.timer`

```ini
[Unit]
Description=VAO Campaign Send Timer

[Timer]
# Lundi, Mercredi, Vendredi à 8h00
OnCalendar=Mon,Wed,Fri 08:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

### `systemd/vao-campaign.service`

```ini
[Unit]
Description=VAO Campaign Daily Send
After=network.target

[Service]
Type=oneshot
User=claude
WorkingDirectory=/home/claude/vao-outreach-bot
EnvironmentFile=/home/claude/vao-outreach-bot/.env
ExecStart=/usr/bin/python3 scripts/run_campaign.py
TimeoutStartSec=7200
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

### `systemd/vao-responses.service`

```ini
[Unit]
Description=VAO Response Checker
After=network.target

[Service]
Type=simple
User=claude
WorkingDirectory=/home/claude/vao-outreach-bot
EnvironmentFile=/home/claude/vao-outreach-bot/.env
ExecStart=/usr/bin/python3 scripts/run_response_check.py --loop --interval=1800
Restart=always
RestartSec=60

[Install]
WantedBy=multi-user.target
```

---

## requirements.txt

```
playwright==1.49.1
playwright-stealth==2.0.3
beautifulsoup4==4.12.3
httpx==0.27.2
supabase==2.11.0
python-dotenv==1.0.1
openai==1.58.1          # Client compatible DeepSeek
resend==2.5.0            # Pour les emails directs (steps 4-5)
imapclient==3.0.1        # Optionnel — Phase 5 : polling IMAP pour automatiser le suivi des réponses
pydantic==2.10.3         # Validation des données
rich==13.9.4             # Affichage terminal (call list, stats)
tenacity==9.0.0          # Retry logic
```

---

## Plan de développement (ordre de build)

### Phase 1 — Fondations (jour 1-2)
1. `db/schema.sql` → exécuter dans Supabase
2. `db/client.py` → wrapper Supabase
3. `config/settings.py` → charger .env
4. `scripts/import_prospects.py` → importer les 21K prospects
5. `services/playwright_manager.py` → lifecycle browser

### Phase 2 — Enrichissement (jour 3-4)
6. `enrichment/site_analyzer.py`
7. `enrichment/form_detector.py`
8. `outreach/field_mapper.py` (heuristique only)
9. `enrichment/scorer.py`
10. `scripts/run_enrichment.py`
→ **TEST** : enrichir 20 prospects, vérifier les scores manuellement

### Phase 3 — Envoi (jour 5-7)
11. `outreach/message_builder.py`
12. `outreach/stealth.py`
13. `outreach/form_filler.py`
14. `outreach/campaign_runner.py`
15. `scripts/run_campaign.py`
→ **TEST** : envoyer 5 formulaires en mode supervisé (headful, pas headless)

### Phase 4 — Tracking & Production (jour 8-10)
16. `tracking/call_list.py`
17. `tracking/stats.py`
18. `scripts/generate_call_list.py`
19. `db/views.sql`
20. `services/deepseek.py` (pour les formulaires ambigus)
21. Setup systemd timers
22. Premier batch réel de 50 prospects

### Phase 5 — Scaling (quand volume > 200/jour)
23. `services/proxy_manager.py` — rotation de proxies IPRoyal résidentiels FR
24. `tracking/response_tracker.py` — automatisation IMAP du suivi des réponses
25. Setup IMAP (Zoho ou autre) pour polling automatique des réponses

---

## Notes pour Claude Code

- **Le bot DOIT fonctionner sans proxy et sans IMAP configurés.** Ces deux dépendances sont optionnelles. Tester d'abord sur IP Hetzner directe, ajouter les proxies uniquement si taux d'échec > 20% à cause de blocages IP.
- **Toujours tester sur des vrais sites de paysagistes** : chercher "paysagiste [ville]" sur Google, prendre les premiers résultats avec un formulaire de contact.
- **Mode headful pour le debug** : lancer Playwright en non-headless pour voir ce qui se passe visuellement.
- **Logs** : utiliser le module `logging` Python avec un format qui inclut le prospect_id pour tracer facilement.
- **Idempotence** : chaque script doit pouvoir être relancé sans créer de doublons (vérifier si l'action a déjà été faite).
- **Graceful shutdown** : le campaign_runner doit pouvoir être interrompu proprement (SIGTERM) sans perdre l'état.
- **Le .env contient des secrets** : ne jamais le commiter. Le `.env.example` est le template.
