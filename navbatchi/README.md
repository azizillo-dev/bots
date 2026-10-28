# Navbatchi Telegram boti

Yotoqxona qavatlari uchun mustaqil axlat navbati. Har qavatning guruh, xona ro'yxati, eslatma vaqti va tarixi alohida. Bajarilgan navbatdagi xona ertangi kunga tayyorlanadi; bajarilmagan navbat ertasi kuni **to'kilmadi** deb qayd etilib, keyingi xonaga o'tadi.

## Ishga tushirish

1. Telegramdagi **@BotFather** orqali bot ochib, token oling.
2. `.env.example` nusxasini `.env` deb saqlang. `BOT_TOKEN` va `ADMIN_IDS` qiymatlarini kiriting. `ADMIN_IDS` — katta adminlarning raqamli Telegram ID lari, vergul bilan ajratiladi. ID ni masalan **@userinfobot** orqali bilish mumkin.
3. Linux serverda loyiha papkasiga kiring va virtual muhit tayyorlang:

   ```bash
   cd /bot/joylashgan/navbatchi
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env
   nano .env
   ```

4. `.env` ichidagi token va admin ID ni saqlagach, botni `screen` ichida ishga tushiring:

   ```bash
   screen -S navbatchi
   cd /bot/joylashgan/navbatchi
   source .venv/bin/activate
   python bot.py
   ```

   Ishlayotgan sessiyadan chiqish: **Ctrl+A**, keyin **D**. Qayta kirish: `screen -r navbatchi`. To'xtatish: sessiyaga kirib **Ctrl+C**.

Bir token bilan botning faqat **bitta nusxasini** ishga tushiring. Bot long polling ishlatadi; server doim yoqilgan bo'lishi kerak. SQLite bazasi loyiha papkasidagi `navbatchi.sqlite3` faylida saqlanadi; uni muntazam zaxiralang.

## Sozlash

1. Botni har qavat guruhiga qo'shing va **admin** qiling. Guruh a'zolari qo'shilishi yoki chiqishini aniqlash uchun admin huquqi zarur.
2. Guruh administratori guruhda `/setup 8` yuboradi. Bot 801–812 xonalarni yaratadi. Boshqa tartib kerak bo'lsa `/rooms 801 802 803` yuboring.
3. Guruhda `/time 08:00` bilan eslatma vaqtini belgilang. Vaqt `.env` dagi `TIMEZONE` ga nisbatan olinadi. Har qavatda alohida vaqt bo'lishi mumkin.
4. Talabalar guruhda `/register` yuboradi yoki botni shaxsiy chatda `/start` bilan ochadi. Guruhdagi admin botga shaxsiy chatda `/admin` yuborib, **A'zolar** orqali ko'ringan talabalarni xonalarga biriktiradi. Talaba shaxsiy chatda `/join 8 803` bilan so'rov yuborishi mumkin; admin **So'rovlar** bo'limida tasdiqlaydi. Bot uning o'sha qavat guruhiga a'zoligini tekshiradi.
5. Har kuni belgilangan vaqtda bot guruhda navbatdagi xonadagilarni belgilab xabar yuboradi. Botga `/start` yuborganlarga shaxsiy eslatma ham keladi. Xona a'zosi yoki admin guruhdagi **To'kdim** tugmasini bosadi.

**Telegram cheklovi:** Bot API guruhning barcha a'zolarini birdan ro'yxat qilib bermaydi. Bot qo'shilish/chiqish hodisalari va bot ko'rgan xabarlar orqali a'zolarni yig'adi. Oldindan guruhda turgan, bot bilan hali aloqaga chiqmagan talaba admin panelida ko'rinmasligi mumkin; unga `/register` yuborishni ayting. Bot guruhga qo'shilish hodisalarini olishi uchun admin bo'lishi kerak. Oddiy xabarlarni ham kuzatish kerak bo'lsa @BotFather dagi **Group Privacy** sozlamasini o'chiring. Shaxsiy xabar uchun talaba botga birinchi bo'lib `/start` yuborishi shart.

## Buyruqlar

| Buyruq | Kim uchun | Vazifa |
|---|---|---|
| `/setup 8` | Guruh admini, guruhda | Guruhni qavatga ulash |
| `/time 08:00` | Qavat admini, guruhda | Kunlik eslatma vaqti |
| `/rooms 801 802 803` | Qavat admini, guruhda | Xonalar tartibi; navbat birinchi xonadan boshlanadi |
| `/register` | Talaba, guruhda | Admin ro'yxatida paydo bo'lish |
| `/join 8 803` | Talaba, shaxsiy chatda | Xonaga biriktirish so'rovi |
| `/my` | Talaba | O'z xonasi va bugungi navbati |
| `/admin`, `/status`, `/history` | Admin | Panel, holat, oxirgi 10 navbat |
| `/overview` | Katta admin | Barcha qavatlar holati |
| `/admin_add ID`, `/admin_remove ID` | Katta admin, shaxsiy chatda | Barcha qavatlar uchun admin |
| `/floor_admin ID 8`, `/floor_admin_remove ID 8` | Katta admin, shaxsiy chatda | Bitta qavat uchun admin |
| `/broadcast groups Matn` | Katta admin, shaxsiy chatda | Barcha ulangan guruhlarga xabar |
| `/broadcast users Matn` | Katta admin, shaxsiy chatda | Botni boshlagan barcha foydalanuvchilarga xabar |

Broadcast yuborishdan oldin tasdiqlash tugmasi chiqadi. Yetib bormagan xabarlar soni admin chatida ko'rsatiladi.

## Tekshirish

`python -m unittest -v` navbat aylanishi, o'tkazib yuborilgan kunlar va tasdiqlash huquqini tekshiradi.

Telegram Bot API: [guruh a'zolari va adminlari](https://core.telegram.org/bots/api#getchatmember), [a'zolik yangilanishlari](https://core.telegram.org/bots/api#chatmemberupdated), [shaxsiy suhbatni boshlash](https://core.telegram.org/bots/features#deep-linking).
