from db import get_user, add_balance

row = await get_user(interaction.guild.id, interaction.user.id)