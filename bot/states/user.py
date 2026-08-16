from aiogram.fsm.state import State, StatesGroup


class WithdrawalForm(StatesGroup):
    amount = State()
