# TG Album DL - Docker Kullanım Kılavuzu

Bu proje, Telegram albüm ve müzik indirme uygulamasını Docker konteyneri içinde çalıştırmayı sağlar.

## 1. Hazırlık

1. `config.json` dosyasında Telegram `api_id` ve `api_hash` değerlerinizin tanımlı olduğundan emin olun (veya `docker-compose.yml` içindeki ortam değişkenlerini kullanın).
2. İndirmeler `./downloads` klasörüne, oturum verileri ise `./data` klasörüne kaydedilir.

## 2. Docker ile Çalıştırma

### Docker Compose ile:
```bash
docker compose up -d --build
```

Durdurmak için:
```bash
docker compose down
```

Logları görmek için:
```bash
docker compose logs -f
```

### Klasik Docker CLI ile:
```bash
# İmajı derle
docker build -t tg-album-dl .

# Konteyneri başlat
docker run -d \
  --name tg-album-dl \
  -p 8765:8765 \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/downloads:/app/downloads \
  -v $(pwd)/config.json:/app/config.json:ro \
  tg-album-dl
```

## 3. Erişim

Tarayıcınızdan şu adrese gidin:
👉 [http://localhost:8765](http://localhost:8765)
