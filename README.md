# Reel Automation Dashboard

Production-ready MVP for a local reel automation control panel built with FastAPI, SQLite, React, Vite, Tailwind CSS, and Axios.

## Project Structure

```text
reel-automation-dashboard/
├── backend/
│   ├── main.py
│   ├── database.py
│   ├── models/
│   ├── routes/
│   ├── services/
│   ├── automation/
│   ├── utils/
│   ├── logs/
│   └── storage/
├── frontend/
│   ├── index.html
│   ├── src/
│   │   ├── App.jsx
│   │   ├── pages/
│   │   ├── components/
│   │   ├── services/
│   │   └── styles/
└── README.md
```

## Backend Setup

1. Open a terminal in `backend/`.
2. Create a virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\activate
```

3. Install dependencies:

```powershell
pip install -r requirements.txt
```

4. Optional: create a `.env` file in `backend/` using `.env.example` and add your API keys:

```env
GEMINI_API_KEY=your_gemini_key
GEMINI_MODEL=gemini-2.5-flash
PEXELS_API_KEY=your_pexels_key
```

If keys are missing, the app still works locally by using a fallback script and generated placeholder clips.

5. Run the API:

```powershell
uvicorn main:app --reload
```

The API will be available at `http://127.0.0.1:8000`.

## Frontend Setup

1. Open a second terminal in `frontend/`.
2. Install dependencies:

```powershell
npm install
```

3. Start the Vite development server:

```powershell
npm run dev
```

The app will be available at `http://localhost:5173`.

## Features

- FastAPI backend with modular routes, services, and automation skeleton
- SQLite persistence for reels, logs, and settings via SQLAlchemy
- Real reel generation pipeline with script, voice, media fetching, and MP4 rendering
- React dashboard with automation controls, reel history, activity logs, and settings management
- Tailwind CSS styling with responsive layout and Axios-based API integration

## API Endpoints

- `GET /health`
- `POST /automation/start`
- `POST /automation/stop`
- `POST /automation/generate`
- `GET /automation/status`
- `GET /reels`
- `GET /reels/{id}`
- `GET /settings`
- `POST /settings`
- `GET /logs`

## Notes

- Reel files, scripts, audio assets, source video clips, music files, and the SQLite database are stored locally under `backend/storage/`.
- `Generate Reel` now creates a real MP4 under `backend/storage/reels/`.
- Gemini and Pexels are optional. If an API key is unavailable or a request fails, the pipeline falls back to local content so the system still works.
- Add optional background tracks to `backend/storage/music/` as `.mp3`, `.wav`, `.m4a`, or `.aac` files for more human-edited reel output.
