# Очередь продуктовых задач

Текущее состояние и критерии заведённых задач ведутся в GitHub Issues. Сверка 27.09.2026: открыты [#1](https://github.com/CrazyEther/robot-market/issues/1), [#2](https://github.com/CrazyEther/robot-market/issues/2), [#4](https://github.com/CrazyEther/robot-market/issues/4), [#6](https://github.com/CrazyEther/robot-market/issues/6), [#8](https://github.com/CrazyEther/robot-market/issues/8), [#10](https://github.com/CrazyEther/robot-market/issues/10) и [#11](https://github.com/CrazyEther/robot-market/issues/11); закрытых issues нет. Старые draft [PR #3](https://github.com/CrazyEther/robot-market/pull/3), [#5](https://github.com/CrazyEther/robot-market/pull/5), [#7](https://github.com/CrazyEther/robot-market/pull/7), [#9](https://github.com/CrazyEther/robot-market/pull/9) закрыты без объединения: они основаны на синтетических данных. Ветки и история сохранены. Интеграция текущего кода отслеживается в #2; нового PR пока нет. Позднее решение заказчика запрещает синтетические деловые данные; формулировки #1/#4/#6/#8 приведены к нему. Таблица ниже — карта объёма, не второй tracker.

| Код | Пользовательский результат |
| --- | --- |
| [T00](https://github.com/CrazyEther/robot-market/issues/1) | Три предметных процесса и наборы входов с указанием единиц, источников и неизвестных |
| [T01](https://github.com/CrazyEther/robot-market/issues/2) | Web/API/БД, выбор объектов, локальная проверка и первый HTTPS предпросмотр |
| [T02](https://github.com/CrazyEther/robot-market/issues/6) | Проекты, вход и изоляция данных пользователей |
| [T03](https://github.com/CrazyEther/robot-market/issues/8) | Ввод и импорт паспортов объекта |
| [T04](https://github.com/CrazyEther/robot-market/issues/10) | Импорт каталога с сохранением происхождения каждой записи |
| [T05–T07](https://github.com/CrazyEther/robot-market/issues/4) | Объяснимый подбор отдельно для склада, аэропорта и больницы; issue #4 покрывает первый срез выбора, а не полную предметную приёмку |
| T08–T10 | Предметные топологии и расчёт парка для каждого объекта |
| T11–T13 | Общая финансовая модель и три объектных применения |
| [T14](https://github.com/CrazyEther/robot-market/issues/11), T15–T16 | Событийная 2D-симуляция для каждого объекта; #11 относится к складу, аэропорт и медучреждение остаются самостоятельными предметными задачами |
| T17 | What-if и версии сценариев |
| T18 | PDF/CSV и кадр моделирования |
| T19 | Администрирование каталога и происхождения данных |
| T20 | Полная трёхобъектная приёмка и опубликованная демонстрация |

Для T01: `docker compose up --build -d` должен поднимать Django и PostgreSQL с проверенным каталогом v4, предоставленным отдельно от Git. `python manage.py test` проверяет API, русские страницы и отказ `/ready` без каталога. Для T00: каждый параметр должен иметь единицу, происхождение и статус, а закрытые исходные файлы не должны попасть в публичный репозиторий.
