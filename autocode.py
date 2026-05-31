
def _startup_sales_scan(cardinal):
    """
    При старте сканирует продажи за последние 30 дней через cardinal.account.get_sales().
    Для каждой продажи у которой:
      - название лота содержит часы/дни (_parse_hours > 0)
      - аренда ещё не истекла (purchase_ts + hours*3600 > now)
      - аренда ещё не записана в rentals.json
    создаёт запись в rentals.json.
    Позволяет восстановить активные аренды после рестарта Cardinal.
    """
    time.sleep(10)
    try:
        acc_obj = cardinal.account
        now     = time.time()
        cutoff  = now - 30 * 24 * 3600

        logger.info("AutoCode: сканирование продаж за 30 дней...")

        all_shortcuts = []
        start_from    = None
        pages         = 0

        while pages < 50:
            try:
                result = acc_obj.get_sales(
                    start_from=start_from,
                    include_paid=True,
                    include_closed=True,
                    include_refunded=False,
                )
                if isinstance(result, tuple):
                    next_id   = result[0]
                    shortcuts = result[1] if len(result) > 1 else []
                else:
                    break

                if not shortcuts:
                    break

                all_shortcuts.extend(shortcuts)
                pages += 1

                last    = shortcuts[-1]
                last_ts = None
                if hasattr(last, "date_ts"):
                    last_ts = last.date_ts
                elif hasattr(last, "date") and last.date:
                    try:
                        last_ts = last.date.timestamp()
                    except Exception:
                        pass

                if last_ts and last_ts < cutoff:
                    break

                if not next_id:
                    break
                start_from = next_id

            except Exception as e:
                logger.warning(f"AutoCode: ошибка при получении страницы продаж: {e}")
                break

        logger.info(f"AutoCode: получено {len(all_shortcuts)} заказов за 30 дней.")

        if not all_shortcuts:
            return

        rents    = rentals()
        accs     = accounts()
        restored = 0
        skipped  = 0

        existing_order_ids = {r.get("order_id") for r in rents.values()}

        for shortcut in all_shortcuts:
            try:
                order_id  = str(getattr(shortcut, "id", "") or getattr(shortcut, "order_id", ""))
                lot_name  = str(getattr(shortcut, "description", "") or getattr(shortcut, "lot_name", "") or "")
                buyer     = str(getattr(shortcut, "buyer_username", "") or getattr(shortcut, "buyer", "") or "")
                lot_id    = str(getattr(shortcut, "lot_id", "") or "")

                # Пробуем получить chat_id напрямую из объекта заказа
                # FunPayAPI хранит его как buyer_id (= id пользователя = id чата в FunPay)
                chat_id = None
                for attr in ("chat_id", "buyer_id", "node_id", "user_id"):
                    val = getattr(shortcut, attr, None)
                    if val:
                        try:
                            chat_id = int(val)
                            break
                        except (TypeError, ValueError):
                            pass

                purchase_ts = None
                if hasattr(shortcut, "date_ts"):
                    purchase_ts = shortcut.date_ts
                elif hasattr(shortcut, "date") and shortcut.date:
                    try:
                        purchase_ts = shortcut.date.timestamp()
                    except Exception:
                        pass

                if not purchase_ts:
                    skipped += 1
                    continue

                if purchase_ts < cutoff:
                    skipped += 1
                    continue

                if order_id and order_id in existing_order_ids:
                    skipped += 1
                    continue

                hours = _parse_hours(lot_name)
                if not hours:
                    skipped += 1
                    continue

                expires_at = purchase_ts + hours * 3600

                if expires_at <= now:
                    skipped += 1
                    continue

                acc_email = None
                for a in accs:
                    if lot_id in a.get("lot_ids", []) or not a.get("lot_ids"):
                        acc_email = a["email"]
                        break

                if not acc_email:
                    skipped += 1
                    continue

                # Если chat_id не нашли в объекте — пробуем get_chat_by_name как fallback
                chat_name = buyer
                if not chat_id:
                    try:
                        chat = acc_obj.get_chat_by_name(buyer, True)
                        if chat:
                            chat_id   = chat.id
                            chat_name = getattr(chat, "name", buyer)
                    except Exception:
                        pass

                if not chat_id:
                    # Последний fallback: используем buyer_username как chat_name,
                    # chat_id оставляем None — запись всё равно создаём,
                    # но без возможности отправить сообщение
                    logger.debug(
                        f"AutoCode: chat_id не найден для {buyer}, "
                        f"аренда восстановлена без chat_id."
                    )

                key = order_id or str(uuid_lib.uuid4())
                rents[key] = {
                    "order_key":   key,
                    "buyer":       buyer,
                    "chat_id":     chat_id,
                    "chat_name":   chat_name,
                    "email":       acc_email,
                    "lot_id":      lot_id,
                    "order_id":    order_id,
                    "purchase_ts": purchase_ts,
                    "expires_at":  expires_at,
                    "hours":       hours,
                    "restored":    True,
                }
                existing_order_ids.add(order_id)
                restored += 1
                logger.info(
                    f"AutoCode: восстановлена аренда {buyer} | {acc_email} | "
                    f"{hours}ч | до {_fmt_time(expires_at)} | order={order_id} | chat_id={chat_id}"
                )

            except Exception as e:
                logger.warning(f"AutoCode: ошибка обработки заказа: {e}")
                continue

        if restored > 0:
            save_rentals(rents)

        logger.info(
            f"AutoCode: сканирование завершено. "
            f"Восстановлено: {restored}, пропущено: {skipped}."
        )

        if restored > 0:
            _notify_tg(cardinal,
                f"✅ AutoCode: при старте восстановлено {restored} активных аренд "
                f"из истории продаж за 30 дней."
            )

    except Exception as e:
        logger.error(f"AutoCode: ошибка сканирования продаж при старте: {e}")
