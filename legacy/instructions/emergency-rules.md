# Инструкция по правилам аварий

Условие — выражение для `simpleeval`: сравнения, `and`/`or`/`not`, `in` для проверки текста аварии в списке bitfield.

Имена переменных должны совпадать с полями `name` в `settings.json` (включая имена с точками, например `Gov.Reg.Value`).

Для дискретов и аналогов — те же имена; для ошибок в `in` — строки из секций `bits` у bitfield.

Допустимы функции: `abs`, `min`, `max`, `round`, `len`.

Правило должно возвращать `True` или `False`.

## Примеры

- `'1670 EmergStop DI24' in active_alarms`
- `('Start Fail' in active_alarms) and (RPM < 300)`
- `('1770 Low Oil Press. DI92' in active_alarms) and Engine_running`
