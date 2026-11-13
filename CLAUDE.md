# CLAUDE.md — Техническая документация проекта ParkVision

## Общая информация о проекте

**Название:** ParkVision — Система мониторинга и прогнозирования занятости городских парковок  
**Тип:** GovTech / Smart City / Computer Vision  
**Формат:** Рабочий MVP для социального хакатона  
**Команда:** 4 участника  
**Срок:** 48 часов  
**Дата старта:** 13 ноября 2026  

## Проблема и решение

### Проблема
Водители в Владивостоке теряют время при поиске свободных парковочных мест. Наличие парковки на карте не означает доступность места в момент приезда.

### Решение
Система определяет занятость конкретных мест по изображениям камер видеонаблюдения, показывает актуальные статусы на карте и рекомендует место с учётом приоритета пользователя.

### Граница MVP
- Одна пилотная парковка (10-30 мест)
- Два режима выбора места
- Обновление занятости в реальном времени
- Маршрут до точки подъезда
- Отображение точного места на схеме

## Технологический стек

```yaml
Mobile Frontend:
  - Flutter + Dart
  - 2ГИС Flutter SDK Full
  - Поддержка: Android / iOS

Backend:
  - Python 3.10+
  - FastAPI
  - PostgreSQL 14+ + PostGIS
  - Docker Compose

Computer Vision:
  - Python + OpenCV
  - Лёгкая CNN / ONNX Runtime
  - Периодическая обработка кадров (~15 сек)

Прогнозирование:
  - Базовая эвристика (MVP)
  - XGBoost (после накопления истории)

Админ-панель:
  - Streamlit (MVP)

Тестирование:
  - Pytest
  - Интеграционные тесты
```

## Архитектура системы

### Поток данных

```
Камеры / тестовые кадры
         ↓
Сервис захвата кадров
         ↓
OpenCV / CNN классификация
         ↓
Статусы парковочных мест
         ↓
FastAPI ←----→ PostgreSQL + PostGIS
    |              ↓
    |         Панель админа
    ↓
Flutter-приложение ←--→ 2ГИС SDK
    ↓
Рекомендация + маршрут + схема места
```

### Ключевые принципы архитектуры

1. **Разделение ответственности:** CV-модель, backend и мобильное приложение независимы
2. **Кэширование:** Backend возвращает статусы из БД/кэша, не запускает CV на каждый запрос
3. **Масштабируемость:** Поддержка нескольких камер и множества мест на один видеопоток
4. **Явная обработка неопределённости:** Статусы `free`, `occupied`, `unknown`
5. **Безопасность:** Секреты камер, API-ключи защищены; нет прямого доступа к камерам из приложения

## Модель данных

### ParkingLot (Парковка)
```python
{
    "id": "P-001",
    "name": "Парковка у ТЦ Clover House",
    "latitude": 43.115141,
    "longitude": 131.885341,
    "capacity": 25,
    "parking_type": "free",  # free | paid | restricted
    "access_rules": "круглосуточно",
    "entrance_lat": 43.115200,
    "entrance_lon": 131.885400
}
```

### ParkingSpot (Парковочное место)
```python
{
    "id": "S-014",
    "parking_lot_id": "P-001",
    "camera_id": "CAM-001",
    "latitude": 43.115141,
    "longitude": 131.885341,
    "approach_lat": 43.115200,  # точка подъезда
    "approach_lon": 131.885400,
    "roi_polygon": [[100, 100], [200, 100], [200, 200], [100, 200]],  # на кадре
    "spot_type": "standard",  # standard | disabled | electric
    "status": "free",  # free | occupied | unknown
    "confidence": 0.97,
    "updated_at": "2026-11-13T10:30:00+10:00"
}
```

### Camera (Камера)
```python
{
    "id": "CAM-001",
    "parking_lot_id": "P-001",
    "source_reference": "rtsp://...",  # или путь к тестовым кадрам
    "status": "active",  # active | inactive | error
    "last_frame_at": "2026-11-13T10:30:15+10:00"
}
```

### OccupancyHistory (История занятости)
```python
{
    "id": 1234,
    "spot_id": "S-014",
    "status": "occupied",
    "confidence": 0.95,
    "observed_at": "2026-11-13T10:29:45+10:00"
}
```

## Backend API

### Базовый URL
```
http://localhost:8000/api/v1
```

### Endpoints для мобильного приложения

#### 1. GET /parkings
Получить список всех парковок с геометрией

**Response:**
```json
{
  "parkings": [
    {
      "id": "P-001",
      "name": "Парковка у ТЦ Clover House",
      "latitude": 43.115141,
      "longitude": 131.885341,
      "capacity": 25,
      "free_spots": 7,
      "occupied_spots": 18,
      "unknown_spots": 0,
      "parking_type": "free",
      "last_update": "2026-11-13T10:30:00+10:00"
    }
  ]
}
```

#### 2. GET /parkings/{id}
Получить детали конкретной парковки

**Response:**
```json
{
  "id": "P-001",
  "name": "Парковка у ТЦ Clover House",
  "latitude": 43.115141,
  "longitude": 131.885341,
  "capacity": 25,
  "free_spots": 7,
  "parking_type": "free",
  "access_rules": "круглосуточно",
  "entrance": {
    "lat": 43.115200,
    "lon": 131.885400
  },
  "updated_at": "2026-11-13T10:30:00+10:00"
}
```

#### 3. GET /parkings/{id}/spots
Получить все места на парковке

**Response:**
```json
{
  "spots": [
    {
      "spot_id": "S-014",
      "parking_id": "P-001",
      "status": "free",
      "confidence": 0.97,
      "location": {"lat": 43.115141, "lon": 131.885341},
      "approach_point": {"lat": 43.115200, "lon": 131.885400},
      "updated_at": "2026-11-13T10:30:00+10:00",
      "forecast_available": false
    }
  ]
}
```

#### 4. GET /spots/{id}
Получить состояние конкретного места

**Response:**
```json
{
  "spot_id": "S-014",
  "parking_id": "P-001",
  "status": "free",
  "confidence": 0.97,
  "location": {"lat": 43.115141, "lon": 131.885341},
  "approach_point": {"lat": 43.115200, "lon": 131.885400},
  "forecast_available": false,
  "updated_at": "2026-11-13T10:30:00+10:00"
}
```

#### 5. POST /recommendations
Получить рекомендации парковочных мест

**Request:**
```json
{
  "user_lat": 43.110000,
  "user_lon": 131.880000,
  "destination_lat": 43.115500,
  "destination_lon": 131.885500,
  "search_mode": "closest",  # "closest" | "fastest"
  "arrival_eta_minutes": 10
}
```

**Response:**
```json
{
  "recommendations": [
    {
      "spot_id": "S-014",
      "parking_id": "P-001",
      "parking_name": "Парковка у ТЦ Clover House",
      "status": "free",
      "confidence": 0.97,
      "location": {"lat": 43.115141, "lon": 131.885341},
      "approach_point": {"lat": 43.115200, "lon": 131.885400},
      "walking_distance_m": 120,
      "driving_eta_min": 8,
      "score": 0.92,
      "updated_at": "2026-11-13T10:30:00+10:00"
    }
  ],
  "search_mode": "closest"
}
```

### Внутренние endpoints (для CV-сервиса)

#### POST /internal/occupancy
Приём статусов от CV-модуля (требует авторизации)

**Headers:**
```
Authorization: Bearer <CV_SERVICE_TOKEN>
```

**Request:**
```json
{
  "camera_id": "CAM-001",
  "timestamp": "2026-11-13T10:30:00+10:00",
  "detections": [
    {
      "spot_id": "S-014",
      "status": "free",
      "confidence": 0.97
    },
    {
      "spot_id": "S-015",
      "status": "occupied",
      "confidence": 0.99
    }
  ]
}
```

**Response:**
```json
{
  "status": "accepted",
  "processed": 2,
  "timestamp": "2026-11-13T10:30:01+10:00"
}
```

#### GET /health
Проверка работоспособности сервиса

**Response:**
```json
{
  "status": "healthy",
  "version": "1.0.0",
  "timestamp": "2026-11-13T10:30:00+10:00",
  "database": "connected",
  "cv_service": "active"
}
```

## Алгоритмы рекомендаций

### Режим A: «Ближе к цели» (closest)

**Приоритет:** Минимизация пешеходного расстояния от места парковки до назначения.

**Фильтрация:**
1. Исключить места со статусом `occupied` или `unknown`
2. Исключить недоступные для пользователя (например, для инвалидов)
3. Исключить устаревшие данные (старше 60 секунд)

**Сортировка:**
```python
score = (
    - walking_distance_m * 1.0       # основной фактор
    - (time.now() - updated_at) * 0.1  # штраф за устаревание
    + confidence * 10                  # бонус за уверенность
)
```

### Режим B: «Быстрее припарковаться» (fastest)

**Приоритет:** Минимизация ожидаемого времени до успешной парковки.

**Эвристика (MVP):**
```python
score = (
    - driving_eta_min * 2.0           # время в пути
    - risk_of_occupied * 50           # риск занятости
    + free_spots_count * 5            # альтернативы на парковке
    + confidence * 10                 # уверенность в данных
    - walking_distance_m * 0.3        # расстояние пешком (меньший вес)
)

# risk_of_occupied вычисляется так:
if forecast_available:
    risk = 1.0 - predicted_availability
else:
    # базовая эвристика
    time_since_update = now() - updated_at
    risk = min(time_since_update / 120.0, 0.5)  # растёт со временем
```

**После пилота:** XGBoost на истории изменений статусов.

## Computer Vision Pipeline

### Алгоритм обработки кадра

```python
def process_frame(camera_id: str, frame: np.ndarray) -> List[Detection]:
    """
    1. Получить настройки камеры из БД (ROI для каждого места)
    2. Вырезать ROI для каждого места
    3. Нормализовать изображения под вход модели
    4. Классифицировать: free / occupied / unknown
    5. Стабилизировать результат (3-5 последовательных кадров)
    6. Вернуть детекции
    """
    camera_config = get_camera_config(camera_id)
    detections = []
    
    for spot_config in camera_config.spots:
        roi = extract_roi(frame, spot_config.roi_polygon)
        roi_normalized = preprocess(roi)
        
        prediction = model.predict(roi_normalized)
        status, confidence = parse_prediction(prediction)
        
        detections.append({
            "spot_id": spot_config.spot_id,
            "status": status,
            "confidence": confidence
        })
    
    # Стабилизация: только если 3 кадра подряд показали изменение
    stabilized = stabilize_detections(detections, history)
    
    return stabilized
```

### Оптимизация производительности

1. **Интервал обработки:** ~15 секунд между кадрами (настраиваемо)
2. **ROI-only:** Обрабатываем только размеченные области, не весь кадр
3. **Уменьшение разрешения:** Resize ROI до 224x224 для модели
4. **Пакетная классификация:** Обрабатываем все ROI одним батчем
5. **Change detection:** OpenCV motion detection для пропуска неизменных кадров
6. **Периодическая полная проверка:** Каждые 5 минут независимо от motion detection

### Обработка ошибок CV

**Статус `unknown` выставляется при:**
- Низкая уверенность модели (confidence < 0.7)
- Перекрытие обзора
- Плохое освещение
- Недоступность камеры
- Конфликт между камерами

**Устаревшие данные:**
- Если обновлений нет более 60 секунд → маркировать как устаревшие
- Не показывать устаревшие места как `free`

## Структура репозитория

```
parkvision/
├── backend/
│   ├── app/
│   │   ├── main.py           # FastAPI приложение
│   │   ├── models/           # Pydantic модели
│   │   ├── api/              # Endpoints
│   │   ├── services/         # Бизнес-логика
│   │   └── db/               # База данных
│   ├── requirements.txt
│   └── Dockerfile
│
├── cv-service/
│   ├── main.py               # CV сервис
│   ├── detector.py           # Классификация
│   ├── camera_manager.py     # Управление камерами
│   ├── models/               # ONNX модели
│   └── requirements.txt
│
├── mobile/
│   ├── lib/
│   │   ├── main.dart
│   │   ├── screens/          # Экраны
│   │   ├── widgets/          # Виджеты
│   │   ├── services/         # API клиент
│   │   └── models/           # Модели данных
│   └── pubspec.yaml
│
├── admin-panel/
│   ├── app.py                # Streamlit панель
│   └── requirements.txt
│
├── docker-compose.yml
├── README.md
└── docs/
    ├── CLAUDE.md             # Этот файл
    └── Agents.md             # Организация AI-агентов
```

## Переменные окружения

### Backend (.env)
```bash
DATABASE_URL=postgresql://user:pass@localhost:5432/parkvision
CV_SERVICE_TOKEN=<секретный токен для CV-сервиса>
REDIS_URL=redis://localhost:6379
GISMAP_API_KEY=<ключ 2ГИС API>
```

### CV Service (.env)
```bash
BACKEND_URL=http://backend:8000
CV_SERVICE_TOKEN=<тот же токен>
FRAME_INTERVAL=15  # секунды между кадрами
CONFIDENCE_THRESHOLD=0.7
```

### Mobile (Flutter)
```dart
// lib/config.dart
class Config {
  static const String apiBaseUrl = 'http://localhost:8000/api/v1';
  static const String gisMapApiKey = '<ключ 2ГИС>';
}
```

## Команды для разработки

### Backend
```bash
cd backend
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### CV Service
```bash
cd cv-service
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python main.py
```

### Flutter
```bash
cd mobile
flutter pub get
flutter run
```

### Docker Compose (всё вместе)
```bash
docker-compose up --build
```

## Тестирование

### Backend тесты
```bash
cd backend
pytest tests/ -v --cov=app
```

### Интеграционный тест (сквозной сценарий)
```bash
pytest tests/integration/test_full_scenario.py -v
```

### Flutter тесты
```bash
cd mobile
flutter test
```

## Критерии приёмки MVP

### Обязательные (P0)

- [ ] Flutter-приложение запускается на тестовом устройстве (Android/iOS)
- [ ] Карта показывает одну пилотную парковку с маркером
- [ ] Backend отдаёт статусы конкретных мест с временем обновления
- [ ] CV определяет занятость на тестовых кадрах (accuracy ≥ 90%)
- [ ] Работают оба режима подбора: "Ближе к цели" и "Быстрее припарковаться"
- [ ] Пользователь может выбрать конкретное место по ID
- [ ] Отображается маршрут до точки подъезда
- [ ] Показывается схема парковки с выделенным местом
- [ ] После занятия выбранного места предлагается альтернатива
- [ ] Устаревшие/неопределённые статусы не показываются как `free`

### Желательные (P1)

- [ ] Базовый прогноз занятости к моменту прибытия
- [ ] Автоматическое обновление статусов каждые 15-30 секунд
- [ ] Админ-панель для разметки ROI и просмотра статистики
- [ ] Периодическая проверка и оптимизация запросов

### После хакатона (P2)

- [ ] ML-прогноз на собственной истории
- [ ] Масштабирование на несколько парковок
- [ ] Пользовательские уведомления
- [ ] Экологическая аналитика
- [ ] Монетизация (реклама, подписка)

## Риски и митигация

| Риск | Вероятность | Митигация |
|------|-------------|-----------|
| Нет доступа к камерам | Высокая | Тестовые кадры из согласованного источника |
| Низкая точность CV | Средняя | Статус `unknown` при низкой уверенности, стабилизация результатов |
| Нет истории для прогноза | Высокая | Базовая эвристика без ML, не показывать выдуманные вероятности |
| Нет API 2ГИС | Средняя | Точка на карте + схема подъезда, без полноценной навигации |
| Не успеваем админку | Средняя | Минимальная Streamlit-страница или конфиг в JSON |
| iOS build недоступен | Средняя | Демо на Android, Flutter-код остаётся кроссплатформенным |
| Одно место → несколько водителей | Низкая | Явно указываем, что это рекомендация, не бронь |

## Контакты и ресурсы

### Команда
- **Участник #1:** Computer Vision Engineer
- **Участник #2:** Backend / ML Engineer
- **Участник #3:** Flutter / Maps Developer
- **Участник #4:** Product / Admin / QA / Integration

### Полезные ресурсы
- **2ГИС API:** https://docs.2gis.com/
- **FastAPI документация:** https://fastapi.tiangolo.com/
- **Flutter документация:** https://flutter.dev/docs
- **OpenCV Python:** https://docs.opencv.org/
- **ONNX Runtime:** https://onnxruntime.ai/

### Репозиторий
```bash
git clone <URL репозитория>
cd parkvision
```

## Следующие шаги

### Первые 2 часа (setup)
1. Создать репозиторий и структуру проекта
2. Согласовать JSON-контракты API
3. Настроить Docker Compose
4. Выбрать пилотную парковку и источник тестовых кадров
5. Разделить задачи по участникам

### День 1 (параллельная разработка)
- CV Engineer: Разметка ROI, классификация, тесты на кадрах
- Backend Engineer: FastAPI, БД, endpoints, рекомендации
- Flutter Developer: UI, карта, моки данных
- Product/QA: Разметка пилота, админка, интеграционные тесты

### День 2 (интеграция и финал)
- Утро: Соединение модулей, сквозной сценарий
- День: Исправление багов, оптимизация
- Вечер: Финальная демонстрация, презентация, защита

---

**Версия документа:** 1.0  
**Дата:** 13 ноября 2026  
**Статус:** Готово к хакатону
