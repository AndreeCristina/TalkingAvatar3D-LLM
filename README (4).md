# Talking Avatar 3D – avatar conversațional în Blender

## Descriere

Talking Avatar 3D este o aplicație interactivă realizată în Blender, în care un avatar uman tridimensional răspunde vocal la întrebări introduse de utilizator. Răspunsurile sunt generate local de modelul lingvistic **Qwen3 4B Instruct**, prin **Ollama**.

Avatarul a fost creat și personalizat cu Blender și MPFB. Aplicația include animarea buzelor, clipit automat, mișcări discrete ale capului și o postură relaxată.

## Fișierele proiectului

```text
TalkingAvatar3D_Final/
├── TalkingAvatar_AI_Functional.blend
├── TalkingAvatar_AI_Synchronized_Lips.py
└── README.md
```

- `TalkingAvatar_AI_Functional.blend` – scena Blender, modelul 3D, materialele, scheletul și formele faciale.
- `TalkingAvatar_AI_Synchronized_Lips.py` – panoul de interacțiune, comunicarea cu Ollama, sinteza vocală și animațiile.
- `README.md` – instrucțiuni pentru configurare și rulare.

## Cerințe

- **Windows 11** și o voce engleză disponibilă pentru `System.Speech`.
- **Blender 5.2.3** sau o versiune compatibilă.
- **Ollama** instalat, cu modelul `qwen3:4b-instruct` disponibil local.
- Difuzoare sau căști.

Scriptul utilizează modulele Python incluse în Blender și componente Windows; nu necesită instalarea unor pachete Python suplimentare.

## Instalare și rulare

**1. Pregătirea modelului AI**

Deschideți PowerShell și executați:

```powershell
ollama pull qwen3:4b-instruct
```

Ollama trebuie să ruleze local, la adresa `http://127.0.0.1:11434`. Disponibilitatea serverului poate fi verificată în browser la această adresă; dacă serverul rulează deja, nu este necesară pornirea unei a doua instanțe.

**2. Deschiderea avatarului**

Deschideți fișierul `TalkingAvatar_AI_Functional.blend` în Blender.

**3. Pornirea aplicației**

În Blender, accesați spațiul de lucru **Scripting**. În **Text Editor**, alegeți **Text → Open** și încărcați `TalkingAvatar_AI_Synchronized_Lips.py`, apoi apăsați **Run Script**.

Reveniți în **Layout**. În panoul lateral al ferestrei 3D (tasta `N`), deschideți fila **Talking Avatar**.

**4. Interacțiunea**

Introduceți o întrebare în câmpul de text și utilizați unul dintre butoane:

- **Ask AI** – generează un răspuns cu Ollama, îl afișează și îl redă vocal, împreună cu animațiile avatarului.
- **Speak Without AI** – rostește direct textul introdus.
- **Stop Speaking** – oprește redarea vocală.

Exemplu de întrebare:

```text
Hello Ava! Can you introduce yourself?
```

Răspunsurile sunt generate dinamic, în limba engleză, și pot varia de la o execuție la alta.

## Funcționalități

- Interacțiune text–răspuns prin model lingvistic local.
- Conversia răspunsului text în vorbire cu Windows Text-to-Speech.
- Animația buzelor cu viseme (Shape Keys), ajustată la durata înregistrării audio.
- Clipit automat la intervale variabile.
- Mișcări discrete ale capului în timpul vorbirii.
- Interfață integrată direct în Blender.

## Precizări tehnice

Aplicația funcționează **direct în Blender**, nu în browser. Sincronizarea buzelor este **aproximativă**: animația folosește viseme și durata audio, fără aliniere exactă a fonemelor. Sinteza vocală depinde de vocile disponibile în Windows, iar modelul Ollama trebuie instalat separat.
