import mysql.connector

try:
    connection = mysql.connector.connect(
        host="localhost",
        user="root",
        password="",
        database="procurement_db",
        port=3306
    )

    if connection.is_connected():
        print("SUCCESS: Connected to procurement_db!")

    connection.close()

except mysql.connector.Error as error:
    print("Database connection failed:", error)