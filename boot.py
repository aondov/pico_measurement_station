import storage
import os
import microcontroller

storage.remount("/", False)
microcontroller.nvm[0] = 1

os.remove("data.txt")
os.remove("data.json")