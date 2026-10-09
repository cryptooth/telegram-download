# TG Album DL

Telegram kanallarındaki müzik paylaşımlarını albüm albüm gruplayıp tek tıkla indiren yerel web arayüzü.

## Kurulum

1. https://my.telegram.org → **API development tools** → bir uygulama oluştur, `api_id` ve `api_hash` al.
2. `config.example.json` dosyasını `config.json` olarak kopyala, değerleri doldur.
3. Kur ve çalıştır:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

4. http://127.0.0.1:8765 adresini aç, telefon numaranla giriş yap (kod Telegram uygulamana gelir).

## Kullanım

- **Soldan kanal seç**: son 300 mesaj taranır, ardışık ses dosyaları albüm kartı olarak listelenir.
  Hemen öncesindeki kapak fotoğrafı ve açıklama albüme bağlanır. "Daha eski mesajları tara" ile geriye gidilir.
- **Mesaj linki yapıştır**: Telegram'da mesaja sağ tık → *Copy Message Link*. İlgili albüm vurgulanır.
- Klasör adını karttan düzenleyebilir, "Parçalar" altından tek tek parça seçimini kaldırabilirsin.
- İndirilenler: `download_dir/<Kanal>/<Albüm>/01 - Parça.flac` (+ `cover.jpg`).
  Yarım kalan indirme tekrar başlatılınca biten dosyalar atlanır.

## Dosya / arşiv paylaşımları

Ses dışındaki dosyalar da listelenir ("Dosyalar" filtresi). Aynı arşivin parçaları
(`.part1.rar`, `.r00`, `.7z.001`, `.z01` …) tek kart olur, parça sırasına dizilir ve
eksik parça varsa kartta uyarı çıkar. Dosyalar orijinal adlarıyla kaydedilir.

- Büyük dosyalarda indirme kesilirse ya da uygulamayı kapatırsan, tekrar "İndir" dediğinde
  `.part` dosyasından kaldığı yerden devam eder.
- **İndirince aç:** `brew install unar` (veya `brew install sevenzip`) kuruluysa kartta seçenek
  görünür; parçalar inince arşiv aynı klasörde bir alt klasöre açılır. Arşiv dosyaları silinmez.
- Hız için `cryptg` kurulu olmalı (requirements'ta var); yoksa GB'lık dosyalar çok yavaş iner.

## Ayarlar

| Ayar | Açıklama |
|---|---|
| `group_gap_minutes` | İki ses mesajı arası bu süreden kısaysa aynı albüm sayılır |
| `parallel_jobs` | Aynı anda kaç albüm indirilecek |
| `host` | Ağdaki başka cihazlardan erişmek için `0.0.0.0` (giriş koruması yok, dikkat) |

Oturum `data/tg.session` dosyasında tutulur; bu dosya hesabına erişim demektir, paylaşma.
