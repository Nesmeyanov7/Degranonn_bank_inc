import os
import discord
from discord import app_commands, ButtonStyle
from discord.ui import View, Button, Modal, TextInput, button
from discord.ext import commands
from dotenv import load_dotenv
import aiosqlite
import re

# --- НАСТРОЙКИ ---
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
DB_NAME = 'bank.db'
# ЗАМЕНИ ЭТО НА ТОЧНОЕ НАЗВАНИЕ РОЛИ АДМИНА НА ТВОЕМ СЕРВЕРЕ
ADMIN_ROLE_NAME = "Техник"

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix='!', intents=intents)


# --- БАЗА ДАННЫХ ---
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS users (
                discord_id INTEGER PRIMARY KEY,
                discord_tag TEXT,
                game_nick TEXT,
                account_id TEXT UNIQUE,
                balance INTEGER DEFAULT 0
            )
        ''')
        await db.commit()


async def get_user_data(discord_id):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute('SELECT * FROM users WHERE discord_id = ?', (discord_id,)) as cursor:
            return await cursor.fetchone()


async def update_balance(account_id, amount):
    async with aiosqlite.connect(DB_NAME) as db:
        # Находим пользователя по ID счета
        async with db.execute('SELECT balance FROM users WHERE account_id = ?', (account_id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                return False, "Счет не найден"

            current_balance = row[0]
            new_balance = current_balance + amount

            if new_balance < 0:
                return False, "Недостаточно средств"

            await db.execute('UPDATE users SET balance = ? WHERE account_id = ?', (new_balance, account_id))
            await db.commit()
            return True, new_balance


async def create_user_in_db(discord_id, discord_tag, game_nick, account_id):
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            await db.execute(
                'INSERT INTO users (discord_id, discord_tag, game_nick, account_id, balance) VALUES (?, ?, ?, ?, 0)',
                (discord_id, discord_tag, game_nick, account_id)
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


# --- МОДАЛЬНЫЕ ОКНА ---

class CreateAccountModal(Modal, title="Создание счета"):
    def __init__(self, target_user: discord.Member):
        super().__init__()
        self.target_user = target_user
        self.game_nick = TextInput(
            label="Ник в игре",
            placeholder="Введите ваш игровой никнейм",
            required=True
        )
        self.account_id = TextInput(
            label="ID Счета (10 цифр)",
            placeholder="Только 10 цифр",
            required=True,
            max_length=10,
            min_length=10
        )
        self.add_item(self.game_nick)
        self.add_item(self.account_id)

    async def on_submit(self, interaction: discord.Interaction):
        acc_id = self.account_id.value

        # Проверка на цифры
        if not acc_id.isdigit():
            await interaction.response.send_message("❌ ID счета должен состоять только из цифр!", ephemeral=True)
            return

        success = await create_user_in_db(
            self.target_user.id,
            self.target_user.name,
            self.game_nick.value,
            acc_id
        )

        if success:
            await interaction.response.send_message(
                f"✅ Счет успешно создан!\n"
                f"Пользователь: {self.target_user.name}\n"
                f"Ник в игре: {self.game_nick.value}\n"
                f"ID Счета: `{acc_id}`",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                "❌ Ошибка: Пользователь с таким ID счета или Discord ID уже существует!", ephemeral=True)


class ManageBalanceModal(Modal, title="Управление балансом"):
    def __init__(self, action_type: str, target_user: discord.Member):
        super().__init__()
        self.action_type = action_type
        self.target_user = target_user

        labels = {
            "add": "Сумма для зачисления",
            "remove": "Сумма для списания",
            "set": "Новый баланс"
        }

        self.amount_input = TextInput(
            label=labels.get(action_type, "Сумма"),
            placeholder="Введите сумму числом",
            required=True
        )
        self.account_input = TextInput(
            label="ID Счета получателя",
            placeholder="10 цифр ID счета",
            required=True,
            max_length=10,
            min_length=10
        )

        self.add_item(self.amount_input)
        self.add_item(self.account_input)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            amount = int(self.amount_input.value)
        except ValueError:
            await interaction.response.send_message("❌ Сумма должна быть числом!", ephemeral=True)
            return

        acc_id = self.account_input.value
        if not acc_id.isdigit():
            await interaction.response.send_message("❌ ID счета должен быть числом!", ephemeral=True)
            return

        if self.action_type == "set" and amount < 0:
            await interaction.response.send_message("❌ Баланс не может быть отрицательным!", ephemeral=True)
            return

        if self.action_type == "remove" and amount < 0:
            await interaction.response.send_message("❌ Сумма списания не может быть отрицательной!", ephemeral=True)
            return

        # Для set нам нужно знать текущий баланс, чтобы сделать дельту,
        # но проще сделать запрос в БД внутри update_balance или написать отдельную логику.
        # Здесь упростим: для set мы просто запишем новое значение.

        async with aiosqlite.connect(DB_NAME) as db:
            # Проверяем существование счета
            async with db.execute('SELECT balance FROM users WHERE account_id = ?', (acc_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    await interaction.response.send_message("❌ Счет не найден!", ephemeral=True)
                    return

                current_bal = row[0]
                new_bal = current_bal

                if self.action_type == "add":
                    new_bal += amount
                elif self.action_type == "remove":
                    if current_bal < amount:
                        await interaction.response.send_message("❌ Недостаточно средств на счете!", ephemeral=True)
                        return
                    new_bal -= amount
                elif self.action_type == "set":
                    new_bal = amount

                await db.execute('UPDATE users SET balance = ? WHERE account_id = ?', (new_bal, acc_id))
                await db.commit()

        action_names = {"add": "Зачислено", "remove": "Списано", "set": "Установлен"}
        await interaction.response.send_message(
            f"✅ {action_names[self.action_type]}: **{amount}**\n"
            f"Счет: `{acc_id}`\n"
            f"Новый баланс: **{new_bal}**",
            ephemeral=True
        )


# --- КНОПКИ И ВИДЫ ---

class UserProfileView(View):
    def __init__(self, user_data):
        super().__init__(timeout=None)
        self.user_data = user_data  # (id, tag, nick, acc_id, balance)

    @button(label="Перевести средства", style=ButtonStyle.blurple, emoji="💸")
    async def transfer_btn(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_modal(TransferModal())


class TransferModal(Modal, title="Перевод средств"):
    def __init__(self):
        super().__init__()
        self.target_acc = TextInput(
            label="ID Счета получателя",
            placeholder="10 цифр",
            required=True,
            max_length=10,
            min_length=10
        )
        self.amount = TextInput(
            label="Сумма перевода",
            placeholder="Число",
            required=True
        )
        self.add_item(self.target_acc)
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction):
        sender_id = interaction.user.id
        # Получаем данные отправителя
        sender_data = await get_user_data(sender_id)

        if not sender_data:
            await interaction.response.send_message("❌ У вас нет открытого счета! Обратитесь к администратору.",
                                                    ephemeral=True)
            return

        sender_acc_id = sender_data[3]
        sender_balance = sender_data[4]

        try:
            amount = int(self.amount.value)
        except ValueError:
            await interaction.response.send_message("❌ Неверная сумма!", ephemeral=True)
            return

        if amount <= 0:
            await interaction.response.send_message("❌ Сумма должна быть больше 0!", ephemeral=True)
            return

        if sender_balance < amount:
            await interaction.response.send_message(f"❌ Недостаточно средств! Вам не хватает {amount - sender_balance} монет.", ephemeral=True)
            return

        target_acc = self.target_acc.value
        if not target_acc.isdigit():
            await interaction.response.send_message("❌ Неверный формат ID счета!", ephemeral=True)
            return

        if target_acc == sender_acc_id:
            await interaction.response.send_message("❌ Нельзя перевести деньги самому себе!", ephemeral=True)
            return

        async with aiosqlite.connect(DB_NAME) as db:
            # Проверка получателя
            async with db.execute('SELECT balance FROM users WHERE account_id = ?', (target_acc,)) as cursor:
                receiver = await cursor.fetchone()
                if not receiver:
                    await interaction.response.send_message("❌ Получатель не найден!", ephemeral=True)
                    return

                # Транзакция
                new_sender_bal = sender_balance - amount
                new_receiver_bal = receiver[0] + amount

                await db.execute('UPDATE users SET balance = ? WHERE account_id = ?', (new_sender_bal, sender_acc_id))
                await db.execute('UPDATE users SET balance = ? WHERE account_id = ?', (new_receiver_bal, target_acc))
                await db.commit()

        await interaction.response.send_message(
            f"✅ Перевод успешен!\nСумма: **{amount}**\nПолучатель: `{target_acc}`\nВаш остаток: **{new_sender_bal}**",
            ephemeral=True
        )


class AdminPanelView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @button(label="Создать счет (выбери юзера)", style=ButtonStyle.green, emoji="📝")
    async def create_account_btn(self, interaction: discord.Interaction, button: Button):
        # Эта кнопка теперь просто подсказка, реальное действие через команду /admin_create
        await interaction.response.send_message(
            "ℹ️ Чтобы создать счет, используйте команду:\n"
            "`/admin_create @пользователь`\n"
            "После этого откроется окно для ввода ника и ID.",
            ephemeral=True
        )

    @button(label="Операции с балансом", style=ButtonStyle.blurple, emoji="💰")
    async def manage_balance_btn(self, interaction: discord.Interaction, button: Button):
        # Аналогично, требуем команду с выбором юзера
        await interaction.response.send_message(
            "ℹ️ Для управления балансом используйте:\n"
            "`/admin_add @пользователь`\n"
            "`/admin_remove @пользователь`\n"
            "`/admin_set @пользователь`",
            ephemeral=True
        )


# --- КОМАНДЫ ---

@bot.event
async def on_ready():
    print(f'Бот запущен как {bot.user}')
    await init_db()
    try:
        synced = await bot.tree.sync()
        print(f"Синхронизировано {len(synced)} команд.")
    except Exception as e:
        print(f"Ошибка синхронизации: {e}")


def check_admin_role(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        return False
    # Проверка на владельца сервера
    if interaction.user.id == interaction.guild.owner_id:
        return True
    # Проверка на роль
    return any(role.name == ADMIN_ROLE_NAME for role in interaction.user.roles)


# Команда старта для игроков
@bot.tree.command(name="start", description="Открыть личный кабинет")
async def start_cmd(interaction: discord.Interaction):
    user_data = await get_user_data(interaction.user.id)

    if not user_data:
        await interaction.response.send_message(
            "❌ У вас еще нет банковского счета.\nОбратитесь к администратору для создания.",
            ephemeral=True
        )
        return

    # user_data: (id, tag, nick, acc_id, balance)
    embed = discord.Embed(title="🏦 Личный кабинет", color=discord.Color.green())
    embed.add_field(name="Пользователь", value=f"{user_data[1]}", inline=False)
    embed.add_field(name="Ник в игре", value=f"{user_data[2]}", inline=True)
    embed.add_field(name="ID Счета", value=f"`{user_data[3]}`", inline=True)
    embed.add_field(name="Баланс", value=f"**{user_data[4]}** монет", inline=False)

    await interaction.response.send_message(embed=embed, view=UserProfileView(user_data), ephemeral=True)


# Админ команды
@bot.tree.command(name="admin_panel", description="Панель администратора")
@app_commands.check(check_admin_role)
async def admin_panel_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="🛡️ Панель Администратора", color=discord.Color.red())
    embed.add_field(name="Инструкция", value="Используйте команды ниже для работы со счетами.", inline=False)
    embed.add_field(name="Создание", value="/admin_create @user", inline=False)
    embed.add_field(name="Баланс", value="/admin_add, /admin_remove, /admin_set", inline=False)

    await interaction.response.send_message(embed=embed, view=AdminPanelView(), ephemeral=True)


@bot.tree.command(name="admin_create", description="Создать счет пользователю")
@app_commands.describe(user="Выберите пользователя Discord")
@app_commands.check(check_admin_role)
async def admin_create_cmd(interaction: discord.Interaction, user: discord.Member):
    # Проверяем, есть ли уже счет
    data = await get_user_data(user.id)
    if data:
        await interaction.response.send_message(f"⚠️ У пользователя {user.name} уже есть счет ({data[3]}).",
                                                ephemeral=True)
        return

    await interaction.response.send_modal(CreateAccountModal(user))


@bot.tree.command(name="admin_add", description="Зачислить средства")
@app_commands.describe(user="Пользователь")
@app_commands.check(check_admin_role)
async def admin_add_cmd(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.send_modal(ManageBalanceModal("add", user))


@bot.tree.command(name="admin_remove", description="Списать средства")
@app_commands.describe(user="Пользователь")
@app_commands.check(check_admin_role)
async def admin_remove_cmd(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.send_modal(ManageBalanceModal("remove", user))


@bot.tree.command(name="admin_set", description="Установить баланс")
@app_commands.describe(user="Пользователь")
@app_commands.check(check_admin_role)
async def admin_set_cmd(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.send_modal(ManageBalanceModal("set", user))


@admin_panel_cmd.error
@admin_create_cmd.error
@admin_add_cmd.error
@admin_remove_cmd.error
@admin_set_cmd.error
async def admin_error_handler(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        await interaction.response.send_message("❌ У вас нет прав администратора для этой команды.", ephemeral=True)
    else:
        await interaction.response.send_message(f"❌ Ошибка: {error}", ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        print("Ошибка: Токен не найден!")
    else:
        bot.run(TOKEN)