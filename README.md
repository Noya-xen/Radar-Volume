# Radar Volume Multi-Chain

Script terpisah untuk volume radar. Script ini tidak memanggil GMGN dan tidak mengubah radar Signal.
Project: [Noya-xen/Radar-Volume](https://github.com/Noya-xen/Radar-Volume)

## Alur scan

1. Ambil maksimal 25 kandidat per chain. BSC/Ethereum memakai GeckoTerminal Trending Pools; Robinhood memakai ranking token DexPaprika berdasarkan volume 24 jam.
2. Deduplikasi token berdasarkan alamat kontrak dan abaikan token saham dari symbol/nama token. Pair/quote token tidak dipakai untuk filter saham.
3. Gunakan market cap dari GeckoTerminal; bila kosong, lakukan fallback DEX Screener secara batch per chain. Robinhood selalu memakai DEX Screener untuk market cap.
4. Abaikan MC yang tidak diketahui (default), di bawah $500 ribu, atau di atas $50 juta. Filter MC dijalankan sebelum penilaian volume.
5. BSC/Ethereum memakai volume USD `m15` dan `h1` dari GeckoTerminal. Robinhood memakai volume USD token agregat DexPaprika yang tepat untuk 15m dan 1h. Kandidat Robinhood yang volume 1h-nya di bawah $500 ribu dilewati karena 15m tidak mungkin memenuhi ambang $500 ribu; detail token hanya diminta untuk kandidat yang masih mungkin memberi alert.
6. Setelah semua chain selesai, tunggu 180 detik lalu mulai putaran baru. Tidak ada request paralel.

GeckoTerminal mengembalikan hingga 20 pool per halaman; script mengambil halaman berikutnya bila perlu untuk mengumpulkan 25 token unik. Untuk Robinhood, detail DexPaprika diminta serial dan hanya setelah filter market cap, sehingga token di luar batas tidak menghabiskan request volume.

Catatan: volume GeckoTerminal untuk BSC/Ethereum adalah milik pool trending terpilih; volume Robinhood dari DexPaprika merupakan ringkasan token lintas pool. API gratis DexPaprika menyediakan 100K kredit/bulan dan 30 request/menit dengan API key; tanpa key, script memperlambat request mengikuti batas publik 15 request/menit. Kandidat dengan volume 1h ≥ $500 ribu dapat memerlukan satu detail request per token; batas kredit bulanan tetap perlu dipantau.

Dengan konfigurasi contoh ada tiga chain dan maksimal 25 kandidat per chain. GeckoTerminal public API dijaga pada 8 request/menit (jeda 7,5 detik); DEX Screener dipakai untuk fallback MC dan dibatch maksimal 30 alamat per request. Siklus tetap serial, kemudian cooldown 3 menit dihitung setelah putaran selesai.

## Setup lokal/VPS

```bash
cd volume-radar
cp volume.env.example volume.env
nano volume.env
python3 volume-radar.py --once
```

Isi tujuan Telegram dan buat API key gratis DexPaprika di [console.dexpaprika.com](https://console.dexpaprika.com), lalu masukkan ke `DEXPAPRIKA_API_KEY`. Key tidak memerlukan kartu kredit. Default chain adalah BSC, Robinhood, dan Ethereum; mapping dapat diubah melalui `GECKO_NETWORK_MAP`, `DEX_CHAIN_MAP`, dan `DEXPAPRIKA_NETWORK_MAP`.

Setelah pengecekan satu putaran, jalankan siklus berulang:

```bash
python3 volume-radar.py
```

Untuk menjalankan terus di VPS, gunakan `volume-radar.service` sebagai template systemd. Sesuaikan `User`, `WorkingDirectory`, dan path pada unit dengan lokasi checkout di VPS, lalu:

```bash
sudo cp volume-radar.service /etc/systemd/system/volume-radar.service
sudo systemctl daemon-reload
sudo systemctl enable --now volume-radar.service
sudo journalctl -u volume-radar.service -f
```

Stop service:

```bash
sudo systemctl disable --now volume-radar.service
```

State alert dan waktu siklus disimpan di `runtime-state.json` di folder ini. File konfigurasi Telegram dan state diabaikan Git.
