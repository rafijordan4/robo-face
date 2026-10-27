# Robo Face v2

Muka lucu di HP (landscape) yang ikut mouse PC Windows, plus asisten Gemini yang bisa mengendalikan Chrome
lewat kursor AI kuning.

```
pc/          server Python (jalan di PC)
chrome-ext/  ekstensi Chrome (status video/password/notif + agen AI)
android/     project APK (build lewat GitHub Actions)
```

## PC
```
cd pc
pip install websockets
python server.py
```
Catat IP dan PIN. Izinkan firewall (Private network).

## Chrome
chrome://extensions > Developer mode > Load unpacked > folder `chrome-ext`
(kalau sudah terpasang: klik tombol reload di kartunya, lalu refresh tab).

## APK
Push ke GitHub > Actions > Build APK > download artifact `robo-face-apk`.

## Di HP
Tahan layar 1 detik: isi IP PC, PIN, API key Gemini (aistudio.google.com/apikey), lalu Simpan.
- Tap 2x: mic on/off. Tap 4x: tidur, tap 1x: bangun.
- Panah di kanan: panel ketik. Kotak kiri bawah: layar PC (tombol - dan + untuk ukuran, x untuk tutup).
- Tanpa API key: mode perintah biasa ("buka tab baru", "cari lofi di youtube", ...).

## Layar PC dan mouse HP
- Layar PC dikirim sebagai JPEG (GDI+ bawaan Windows). Tombol `30fps`/`60fps` di bar layar. Console server mencetak fps asli tiap 5 detik.
- Mouse HP (kotak di samping layar): geser = gerak, ketuk = klik kiri, 2 jari geser = scroll, ketuk 2 jari = klik kanan,
  tombol Kiri/Kanan bisa ditahan (tahan Kiri + geser = seret), tombol atas/bawah = scroll.
- Windows cuma punya satu kursor asli, jadi mouse PC dan mouse HP berbagi kursor yang sama. Penanda di layar:
  merah = digerakkan dari PC, cyan = dari HP, kuning = kursor AI (di dalam Chrome).

## Aturan keamanan AI
- Kursor kuning hanya overlay di halaman Chrome, tidak menggerakkan mouse Windows.
- Tidak mengisi password, OTP, PIN, kartu. Tidak membuka link download/skrip.
- Aksi seperti bayar, hapus, logout butuh konfirmasi dari kamu.
- Tombol merah "Stop AI" muncul di halaman, menghentikan semua aksi.
- Buka aplikasi hanya dari shortcut Start Menu/Desktop; cmd, PowerShell, terminal, regedit, dll diblokir.
