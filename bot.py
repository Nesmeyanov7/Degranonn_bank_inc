import os
import discord
from discord.ext import commands
from dotenv import load_dotenv

# Загрузка переменных окружения из файла .env
load_dotenv()

TOKEN = os.getenv('DISCORD_TOKEN')

if not TOKEN:
    print("Ошибка: Токен не найден. Проверьте файл .env")
    exit()

# Настройка интентов
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

bot = commands.Bot(command_prefix='!', intents=intents)

# Хранилище балансов (в памяти)
balances = {}


def get_balance(user_id):
    return balances.get(user_id, 0)


def set_balance(user_id, amount):
    balances[user_id] = amount


def add_balance(user_id, amount):
    balances[user_id] = get_balance(user_id) + amount


def remove_balance(user_id, amount):
    current = get_balance(user_id)
    if current >= amount:
        balances[user_id] = current - amount
        return True
    return False


@bot.event
async def on_ready():
    print(f'Бот запущен как {bot.user}')
    try:
        synced = await bot.tree.sync()
        print(f"Синхронизировано {len(synced)} слэш-команд.")
    except Exception as e:
        print(f"Ошибка синхронизации команд: {e}")


# --- Слэш команды ---

@bot.tree.command(name="баланс", description="Показать баланс пользователя")
async def balance(ctx, user: discord.User = None):
    if user is None:
        user = ctx.user

    amount = get_balance(user.id)
    # Используем .name вместо .mention, чтобы не пинговать
    await ctx.response.send_message(f"💰 Баланс пользователя **{user.name}**: **{amount}** монет.", ephemeral=False)


@bot.tree.command(name="set", description="Установить баланс пользователю (Только админы)")
async def set_bal(ctx, user: discord.User, amount: int):
    if not ctx.user.guild_permissions.administrator and ctx.user.id != ctx.guild.owner_id:
        await ctx.response.send_message("❌ У вас нет прав для использования этой команды.", ephemeral=True)
        return

    set_balance(user.id, amount)
    await ctx.response.send_message(f"✅ Баланс пользователя **{user.name}** установлен на **{amount}** монет.")


@bot.tree.command(name="add", description="Добавить деньги пользователю (Только админы)")
async def add_bal(ctx, user: discord.User, amount: int):
    if not ctx.user.guild_permissions.administrator and ctx.user.id != ctx.guild.owner_id:
        await ctx.response.send_message("❌ У вас нет прав для использования этой команды.", ephemeral=True)
        return

    if amount < 0:
        await ctx.response.send_message("❌ Сумма должна быть положительной.", ephemeral=True)
        return

    add_balance(user.id, amount)
    new_bal = get_balance(user.id)
    await ctx.response.send_message(
        f"➕ Добавлено **{amount}** монет пользователю **{user.name}**. Новый баланс: **{new_bal}**.")


@bot.tree.command(name="remove", description="Убрать деньги у пользователя (Только админы)")
async def remove_bal(ctx, user: discord.User, amount: int):
    if not ctx.user.guild_permissions.administrator and ctx.user.id != ctx.guild.owner_id:
        await ctx.response.send_message("❌ У вас нет прав для использования этой команды.", ephemeral=True)
        return

    if amount < 0:
        await ctx.response.send_message("❌ Сумма должна быть положительной.", ephemeral=True)
        return

    success = remove_balance(user.id, amount)
    if success:
        new_bal = get_balance(user.id)
        await ctx.response.send_message(f"➖ Убрано **{amount}** монет у **{user.name}**. Новый баланс: **{new_bal}**.")
    else:
        await ctx.response.send_message(
            f"❌ У пользователя **{user.name}** недостаточно средств (Текущий баланс: {get_balance(user.id)}).")


# Запуск бота
bot.run(TOKEN)