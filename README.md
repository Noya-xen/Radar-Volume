# Radar Volume Multi-Chain

Script terpisah untuk volume radar. Script ini tidak memanggil GMGN dan tidak mengubah radar Signal.
Project: [Noya-xen/gmgn-Base-BSC](https://github.com/Noya-xen/gmgn-Base-BSC)

## Alur scan

1. Ambil GeckoTerminal Trending Pools per chain, maksimal 25 token unik per chain. Feed memakai durasi `VOLUME_TREND_DURATION` (default `1h`).
2. Deduplikasi token berdasarkan alamat kontrak dan abaikan token saham dari symbol/nama token. Pair/quote token tidak dipakai untuk filter saham.
3. Gunakan market cap dari GeckoTerminal; bila kosong, lakukan fallback DEX Screener secara batch per chain.
4. Abaikan MC yang tidak diketahui (default), di bawah $500 ribu, atau di atas $50 juta. Filter MC dijalankan sebelum penilaian volume.
5. Evaluasi volume USD `m15` dan `h1` yang sudah tersedia di Trending Pools, satu kandidat per satu kandidat. Alert dikirim jika salah satu minimum tercapai.
6. Setelah semua chain selesai, tunggu 180 detik lalu mulai putaran baru. Tidak ada request paralel.

GeckoTerminal mengembalikan hingga 20 pool per halaman; script mengambil halaman berikutnya bila perlu untuk mengumpulkan 25 token unik. Data volume yang dipakai berasal dari kolom `volume_usd.m15` dan `volume_usd.h1`; ini menghindari request OHLCV per token. Market cap yang tidak diverifikasi bisa kosong, sehingga DEX Screener dipakai sebagai fallback; jika kedua sumber tidak memberi MC, kandidat dilewati.

Catatan: volume dan likuiditas GeckoTerminal adalah milik pool trending yang dipilih untuk token tersebut, bukan penjumlahan volume semua pool token di chain. Jika beberapa pool memuat token yang sama, token dideduplikasi dan pool yang muncul lebih dulu (peringkat lebih tinggi) dipakai.

Dengan empat chain dan 25 kandidat per chain, batas maksimum adalah 100 kandidat. GeckoTerminal public API dijaga pada 8 request/menit (jeda 7,5 detik) untuk memberi ruang dari batas dokumentasi sekitar 10 request/menit. DEX Screener dipakai hanya sebagai fallback MC dan dibatch maksimal 30 alamat per request. Lama satu putaran mengikuti jumlah halaman dan fallback yang diperlukan, lalu cooldown 3 menit dihitung setelah putaran selesai.

## Setup lokal/VPS

```bash
cd volume-radar
cp volume.env.example volume.env
nano volume.env
python3 volume-radar.py --once
```

Isi chain pada `VOLUME_CHAINS`, market-cap band, threshold, dan tujuan Telegram. Default empat chain adalah BSC, Base, Ethereum, dan Arbitrum; mapping network GeckoTerminal dan chain DEX Screener dapat diubah melalui `GECKO_NETWORK_MAP` dan `DEX_CHAIN_MAP`.

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
