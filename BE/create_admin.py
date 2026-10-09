from getpass import getpass
from database import create_user

username = input("Username: ")
password = getpass("Password: ")
create_user(username, password)
print(f"User '{username}' berhasil dibuat.")