# HandDance

Game ritme interaktif berbasis gerakan tangan menggunakan kamera. Dimainkan seperti DDR — panah bergulir ke bawah, dan Anda harus melakukan gerakan tangan yang sesuai tepat pada waktunya.

## Cara Bermain

| Gerakan Tangan | Arah Panah |
|---|---|
| Ayun kiri | ← Left |
| Ayun kanan | → Right |
| Ayun ke atas | ↑ Up |
| Ayun ke bawah | ↓ Down |

**Sistem Skor:**
- **PERFECT** (±55 ms): 2 poin + combo
- **GOOD** (±110 ms): 1 poin + combo
- **MISS**: 0 poin, combo reset

---

## Persyaratan

- Docker & Docker Compose
- Webcam di browser (gesture diproses di frontend)
- Speaker / audio output (untuk pemutaran lagu)
- Linux host (untuk akses `/dev/video0` dan `/dev/snd`)

---

## Instalasi & Menjalankan

### 1. Clone / salin proyek

```bash
git clone <repo-url>
cd game_interactive
```

### 2. Tambahkan lagu

Salin file audio MP3/OGG ke folder `music/`:

```bash
cp lagu-saya.mp3 music/
```

*(Opsional)* Buat beatmap kustom di `beatmaps/<nama-file>.json` (lihat format di bawah).
Jika tidak ada beatmap, game akan auto-generate pola berdasarkan BPM.

### 3. Build dan jalankan

```bash
docker compose up --build
```

Lalu buka browser: **http://localhost:5000**

### 4. Atau dengan `docker run` manual

```bash
# Build
docker build -t handdance .

# Jalankan (Linux)
docker run -it --rm \
  --device=/dev/video0 \
  --device=/dev/snd \
  --group-add audio \
  -v $(pwd)/music:/app/backend/music \
  -v $(pwd)/beatmaps:/app/backend/beatmaps \
  -p 5000:5000 \
  handdance
```

---

## Menambah Lagu dari YouTube (dalam game)

1. Buka menu utama
2. Pada kolom **"Tambah Lagu dari YouTube"**, tempel URL YouTube
3. Masukkan BPM lagu (opsional, default 120)
4. Klik **Unduh**
5. Lagu akan muncul di dropdown setelah selesai diunduh

> Memerlukan koneksi internet dan `yt-dlp` + `ffmpeg` (sudah terpasang di Docker image).

## Catatan Kamera

- Kamera dipakai oleh browser via `getUserMedia`, bukan oleh container.
- Jika browser meminta izin kamera, klik `Allow`.
- Panel camera ditampilkan sebagai background di belakang lane game.

---

## Format Beatmap Kustom

Simpan sebagai `beatmaps/<nama-audio>.json`:

```json
{
  "title":      "Nama Lagu",
  "artist":     "Nama Artis",
  "audio_file": "nama-file.mp3",
  "bpm":        128,
  "offset":     0.5,
  "difficulty": "medium",
  "notes": [
    { "time": 1.0,  "direction": "left"  },
    { "time": 1.5,  "direction": "right" },
    { "time": 2.0,  "direction": "up"    },
    { "time": 2.5,  "direction": "down"  }
  ]
}
```

| Field | Tipe | Keterangan |
|---|---|---|
| `title` | string | Judul lagu (tampil di UI) |
| `artist` | string | Nama artis |
| `audio_file` | string | Nama file di folder `music/` |
| `bpm` | number | Beat per menit |
| `offset` | number | Delay awal dalam detik sebelum note pertama |
| `difficulty` | string | `easy` / `medium` / `hard` |
| `notes[].time` | number | Waktu ideal hit dalam detik dari awal lagu |
| `notes[].direction` | string | `left` / `right` / `up` / `down` |

---

## Struktur Proyek

```
game_interactive/
├── backend/
│   ├── main.py             # Flask-SocketIO server & routing
│   ├── hand_tracker.py     # MediaPipe hand gesture detection
│   ├── audio_manager.py    # pygame audio playback & sync clock
│   ├── beatmap_parser.py   # Beatmap JSON parser & auto-generator
│   ├── game_logic.py       # Game loop, note timing, scoring
│   ├── music/              # Audio files (volume mount)
│   └── beatmaps/           # Beatmap JSON files (volume mount)
├── frontend/
│   ├── index.html          # Game UI
│   ├── style.css           # Styling
│   └── script.js           # SocketIO client & rendering
├── music/                  # Host-side music folder
├── beatmaps/               # Host-side beatmap folder
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

---

## Variabel Lingkungan

| Variabel | Default | Keterangan |
|---|---|---|
| `MUSIC_DIR` | `/app/backend/music` | Path folder audio |
| `BEATMAP_DIR` | `/app/backend/beatmaps` | Path folder beatmap |
| `CAMERA_INDEX` | `0` | Index kamera (`/dev/video0` = 0) |
| `SWIPE_THRESHOLD` | `0.12` | Sensitivitas swipe (0.05–0.3) |
| `PORT` | `5000` | Port server |

---

## Troubleshooting

**Kamera tidak terdeteksi**
```bash
ls /dev/video*
# Sesuaikan CAMERA_INDEX atau device path di docker-compose.yml
```

**Tidak ada suara**
```bash
# Pastikan user ada di group audio
docker run ... --group-add audio ...
# Atau coba SDL_AUDIODRIVER=pulse
```

**MediaPipe lambat**
- Kurangi resolusi: edit `hand_tracker.py` → `width=160, height=120`
- Atau naikkan `SWIPE_THRESHOLD` untuk mengurangi false positives

**macOS / Windows**
- Akses kamera langsung ke Docker lebih terbatas.
- Disarankan menjalankan secara lokal (tanpa Docker) untuk development:
  ```bash
  pip install -r requirements.txt
  python backend/main.py
  ```
