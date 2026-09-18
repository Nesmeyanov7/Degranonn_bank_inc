import discord
from discord.ext import commands
from dotenv import load_dotenv
from datetime import datetime
import os

load_dotenv()

TOKEN = os.getenv('DISCORD_TOKEN')

if not TOKEN:
    raise ValueError("Токен не найден! Проверьте файл .env")

# Создаем бота с префиксом команд
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Хранилище балансов пользователей (в памяти)
# В реальном проекте лучше использовать базу данных
balances = {}


def get_balance(user_id: int) -> int:
    """Получить баланс пользователя"""
    return balances.get(user_id, 0)


def set_balance(user_id: int, amount: int) -> int:
    """Установить баланс пользователя"""
    balances[user_id] = amount
    return amount


def add_balance(user_id: int, amount: int) -> int:
    """Прибавить к балансу пользователя"""
    current = get_balance(user_id)
    new_balance = current + amount
    balances[user_id] = new_balance
    return new_balance


def remove_balance(user_id: int, amount: int) -> int:
    """Уменьшить баланс пользователя"""
    current = get_balance(user_id)
    new_balance = max(0, current - amount)  # Не уходим в минус
    balances[user_id] = new_balance
    return new_balance


@bot.event
async def on_ready():
    print(f'Бот запущен как {bot.user}')


# === Команды для управления балансом ===

@bot.command(name='баланс')
async def check_balance(ctx):
    """Проверить свой баланс"""
    balance = get_balance(ctx.author.id)
    await ctx.send(f'💰 Ваш баланс: {balance} монет')


@bot.command(name='set')
async def set_balance_cmd(ctx, amount: int):
    """Установить баланс (только для тестов)"""
    if amount < 0:
        await ctx.send('❌ Сумма не может быть отрицательной!')
        return
    
    set_balance(ctx.author.id, amount)
    await ctx.send(f'✅ Баланс установлен на {amount} монет')


@bot.command(name='add')
async def add_balance_cmd(ctx, amount: int):
    """Прибавить к балансу (только для тестов)"""
    if amount < 0:
        await ctx.send('❌ Сумма не может быть отрицательной!')
        return
    
    new_balance = add_balance(ctx.author.id, amount)
    await ctx.send(f'➕ Добавлено {amount} монет. Новый баланс: {new_balance}')


@bot.command(name='remove')
async def remove_balance_cmd(ctx, amount: int):
    """Уменьшить баланс (только для тестов)"""
    if amount < 0:
        await ctx.send('❌ Сумма не может быть отрицательной!')
        return
    
    new_balance = remove_balance(ctx.author.id, amount)
    await ctx.send(f'➖ Убрано {amount} монет. Новый баланс: {new_balance}')


# Запуск бота
if __name__ == '__main__':
    # ЗАМЕНИТЕ 'YOUR_BOT_TOKEN' на токен вашего бота из Discord Developer Portal
    bot.run(TOKEN)
