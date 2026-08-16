from aiogram.fsm.state import State, StatesGroup


class CategoryForm(StatesGroup):
    name = State()
    rename = State()


class ProductForm(StatesGroup):
    category = State()
    name = State()
    description = State()
    price = State()
    kind = State()
    content = State()


class ProductEditForm(StatesGroup):
    value = State()
    stock = State()
    image = State()


class UserSearchForm(StatesGroup):
    query = State()


class BalanceAdjustForm(StatesGroup):
    amount = State()
    reason = State()


class ManualDeliveryForm(StatesGroup):
    content = State()


class WithdrawalRejectForm(StatesGroup):
    reason = State()


class SettingEditForm(StatesGroup):
    value = State()


class BroadcastForm(StatesGroup):
    content = State()
    buttons = State()
