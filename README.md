# Robo Face

Muka lucu di HP (landscape) yang matanya ngikutin mouse PC Windows.

```
robo-face/
  pc/          server Python (jalan di PC)
  chrome-ext/  ekstensi Chrome (video, password, notif web, perintah tab)
  android/     project APK
  .github/workflows/build.yml   build APK otomatis di GitHub
```

## 1. PC (Windows)
```
cd pc
pip install -r requirements.txt     # Python 3.10-3.12
python server.py
```
Catat **IP PC** dan **PIN** yang muncul. Kalau Windows tanya firewall, izinkan di jaringan Private.
Notif native Windows butuh izin: Settings > Privacy & security > Notifications.

## 2. Ekstensi Chrome
`chrome://extensions` > aktifkan Developer mode > Load unpacked > pilih folder `chrome-ext`.
Server `server.py` harus sudah jalan (ekstensi otomatis nyambung sendiri).

## 3. APK
Push folder ini ke repo GitHub > tab **Actions** > Build APK > download artifact `robo-face-apk` > install `app-debug.apk`.

## 4. Pakai
- HP dan PC satu WiFi atau hotspot HP. **Mau tetap Bluetooth?** Nyalakan Bluetooth tethering di HP, sambungkan PC lewat Bluetooth PAN. IP-nya dipakai sama persis.
- Buka app, tahan layar 1 detik, isi IP PC + PIN, Simpan.
- 2 tap: mic on/off. Ucapkan perintah.
- 4 tap: tidur. 1 tap saat tidur: bangun.

## Perintah suara
- "buka tab baru", "tab baru youtube"
- "tutup tab", "tab berikutnya", "tab sebelumnya", "refresh", "kembali"
- "buka youtube", "buka github.com", "cari resep nasi goreng", "cari lofi di youtube"
- "volume naik/turun", "mute", "jeda", "putar", "lagu berikutnya"
