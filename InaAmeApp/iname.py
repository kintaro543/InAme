import sys
import animations_rc
import inaImages_rc
from PySide6.QtWidgets import QApplication,QPushButton,QWidget,QVBoxLayout,QLineEdit,QPlainTextEdit,QStackedLayout,QLabel,QDialog
from PySide6.QtGui import QPixmap,QCursor,QMovie,QIcon
from PySide6.QtCore import Slot,QSize,Qt,QPoint,QTimer,QElapsedTimer,Signal,QThread,QDateTime,QObject,QLockFile,QDir
import random
from google import genai
from google.genai import types
import os
from dotenv import load_dotenv
import re
import numpy as np
import sqlite3
from pydantic import BaseModel, Field
from pathlib import Path
from queue import Queue
import win32gui
import win32con
import ctypes
# import logging
# logging.basicConfig(level=logging.INFO)
ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Kintakodach.InAme.5")

load_dotenv()

# Holds hwnds for all windows that Ina can stand, then return that list. Excludes Progman and WorkerW windows, as well as invisible windows and windows with no title or title "python"
def get_hwnds():
    hwnds = []
    # Callback for EnumWindows: collects every top-level window handle into hwnds.
    def handle_window(hwnd, extra):
        hwnds.append(hwnd)
    win32gui.EnumWindows(handle_window, None)
    hwnds_real = []
    for hwnd in hwnds:
        window_class = win32gui.GetClassName(hwnd)
        visible = win32gui.IsWindowVisible(hwnd)
        title = win32gui.GetWindowText(hwnd)
        if window_class!="Progman" and window_class!="WorkerW" and visible and title and title!="python":
            hwnds_real.append(hwnd)
    return hwnds_real


# Rewrites first/second-person words in the user's message into "User"/"Ina" (e.g. "my" -> "User's", "you" -> "Ina") so the query matches the third-person wording memories are stored in.
def rewrite_query(text):
    text = re.sub(r"\byou\b", "Ina", text, flags=re.IGNORECASE)
    text = re.sub(r"\byour\b", "Ina's", text, flags=re.IGNORECASE)
    text = re.sub(r"\bI\b", "User", text)
    text = re.sub(r"\bmy\b", "User's", text, flags=re.IGNORECASE)
    text = re.sub(r"\bme\b", "User", text, flags=re.IGNORECASE)

    text = re.sub(r"\bours\b", "User's and Ina's", text, flags=re.IGNORECASE)
    text = re.sub(r"\bour\b", "User and Ina's", text, flags=re.IGNORECASE)
    text = re.sub(r"\bus\b", "User and Ina", text, flags=re.IGNORECASE)
    text = re.sub(r"\bwe\b", "User and Ina", text, flags=re.IGNORECASE)
    return text

create_table = [
    """CREATE TABLE IF NOT EXISTS main.memories(
            memory_id INTEGER PRIMARY KEY,
            text TEXT NOT NULL,
            embedding BLOB NOT NULL,
            time TEXT NOT NULL,
            speaker TEXT NOT NULL
        );""",

    """CREATE TABLE IF NOT EXISTS main.name(
            user_name TEXT NOT NULL
        );"""
]

# Returns the path of the SQLite memory database (~/.ina_memory/ina_memories.db), creating the folder if it doesn't exist.
def get_db_path():
    folder = Path.home() / ".ina_memory"
    folder.mkdir(exist_ok=True)
    return folder / "ina_memories.db"

with sqlite3.connect(get_db_path()) as conn:
            cursor = conn.cursor()
            for table in create_table:
                cursor.execute(table)
            conn.commit()

ids = []
vectors = []
matrix = None
dirty = True
Username = None

for mem_id, blob in conn.execute("SELECT memory_id, embedding FROM memories"):
    ids.append(mem_id)
    vectors.append(np.frombuffer(blob, dtype=np.float32))

cursor.execute("SELECT EXISTS (SELECT 1 FROM name)")
has_rows = cursor.fetchone()[0]

# Stores the user's name in the global Username and saves it to the name table.
def insert_name(conn,name):
    global Username
    Username = name
    cur = conn.cursor()
    insert_statement = """
        INSERT INTO name (user_name)
        VALUES (?);
        """
    cur.execute(insert_statement,(name,))
    conn.commit()

class NameAsk(QDialog):
    # Builds the first-run dialog: Ina's greeting, a text box for the user's name and Ina's picture, frameless, always on top, with a transparent background. Pressing Enter calls insert_name.
    def __init__(self):
        super().__init__()
        self.inputName = QLineEdit()
        self.inputName.returnPressed.connect(self.insert_name)
        self.inaGreeting = QLabel("Oya oya little tako, what's your name")
        self.inaGreeting.setObjectName("inaGreeting")
        pix = QPixmap(":/images/inaWindow.png").scaledToWidth(100, Qt.SmoothTransformation)
        w, h = pix.width(), pix.height()
        self.panel = QWidget(self)
        layout = QVBoxLayout(self.panel)
        layout.addWidget(self.inaGreeting)
        layout.addWidget(self.inputName)
        self.resize(248+w+48,52+h+13)
        self.panel.setGeometry(0,h-52+13,300+48,104)
        self.Inart = QLabel(self)
        self.Inart.setObjectName("Inart")
        self.Inart.setPixmap(pix)
        self.Inart.setGeometry(248+43,0,w,h)
        self.Inart.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.Inart.raise_()

        self.setStyleSheet("""
            QWidget{
                background-color: #A36A9D;
            }

            QLineEdit{
                background-color: #65587D;
                color: #F7CE46;
                border: 1px solid #C79E49;
                outline: none;
                font-family: "Comic Sans MS";
                font-size: 16px;
            }

            QLabel#inaGreeting{
                font-family: "Ink Free";
                font-size: 20px;
                background-color: #65587D;
                color: #F7CE46;
                border: 1px solid #C79E49;
                outline: none;
            }

            QLabel#Inart{
                background-color: transparent; border: none;
            }
        """)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint
            )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

    # Opens a database connection, saves the typed name, and closes the dialog as accepted.
    @Slot()
    def insert_name(self):
        self.conn = sqlite3.connect(get_db_path())
        insert_name(self.conn,self.inputName.text())
        self.accept()


# Returns all stored memory embeddings stacked into one numpy matrix (one row per memory). Rebuilds it only when vectors changed (dirty flag). Returns None if there are no memories yet.
def get_matrix():
    global matrix,dirty
    if dirty and vectors:
        matrix = np.vstack(vectors)
        dirty = False
    return matrix

# Saves new memories to the database, skipping near-duplicates: first drops items more than 0.97 cosine-similar to a later item in the same batch, then drops items more than 0.96 similar to a memory already stored. Adds the remaining memories' ids and embeddings to the in-memory cache.
def insert_memories(conn,text_embed_time_speaker):
    global dirty
    #print([mem[0] for mem in text_embed_time_speaker])
    embeddings = [np.frombuffer(mem[1], dtype=np.float32) for mem in text_embed_time_speaker]
    valid_indexes = [len(text_embed_time_speaker)-1]
    for i in range(len(text_embed_time_speaker)-1):
        check_against = []
        for j in range(i+1,len(text_embed_time_speaker)):
            check_against.append(embeddings[j])
        check_against = np.vstack(check_against)
        checking = embeddings[i]
        cosine_similarities = np.matmul(check_against,checking) / (np.linalg.norm(checking) * np.linalg.norm(check_against,axis=1))
        duplicates = cosine_similarities > 0.97
        if sum(duplicates)==0:
            valid_indexes.append(i)
    #print(valid_indexes)
    check_against = get_matrix()
    noDupes = []
    if check_against is not None:
        for i in valid_indexes:
            checking = embeddings[i]
            cosine_similarities = np.matmul(check_against,checking) / (np.linalg.norm(checking) * np.linalg.norm(check_against,axis=1))
            duplicates = cosine_similarities > 0.96
            if sum(duplicates)==0:
                noDupes.append(text_embed_time_speaker[i])
    else:
        noDupes = [text_embed_time_speaker[i] for i in valid_indexes]
    #print([mem[0] for mem in noDupes])
    if noDupes:
        cur = conn.cursor()
        insert_statement = """
                INSERT INTO memories (text,embedding,time,speaker)
                VALUES (?,?,?,?);
                """
        with conn:
            conn.executemany(insert_statement,noDupes)
        current_maxId = max(ids,default=0)
        for mem in noDupes:
            current_maxId += 1
            ids.append(current_maxId)
            vectors.append(np.frombuffer(mem[1], dtype=np.float32))
        dirty = True


# Fetches the memories with the given ids from the database and splits them into Ina's memories and the user's memories, each as a list of (text, time) pairs.
def relavant_memories(conn,acceptable_ids):
    cur = conn.cursor()
    place_holder = "(" + "?" + ",?"*(len(acceptable_ids)-1) + ")"
    select_statement = f"""
            SELECT
                memory_id,
                text,
                time,
                speaker
            FROM 
                memories
            WHERE
                memory_id IN {place_holder};
            """
    cur.execute(select_statement,([int(k) for k in acceptable_ids]))
    relavant_mems = cur.fetchall()
    relavant_ina = []
    relavant_user = []
    for mem in relavant_mems:
        if mem[-1] == "ina":
            relavant_ina.append((mem[1],mem[2]))
        else:
            relavant_user.append((mem[1],mem[2]))
    return relavant_ina,relavant_user

class Memory(BaseModel):
    text: str = Field(description="text of the memory. Holds a single fact")

class Memories(BaseModel):
    user_memories: list[Memory] = Field(description="lasting facts worth remembering, or promises and commitments which are said by user. Write one item per fact; empty if there are none")
    ina_memories: list[Memory] = Field(description="promises or commitments Ina made to user, or facts Ina told user about herself that she should stay consistent about. Write one item per fact; empty if there are none")

# query = """
# User : I like cookies and pies
# Ina : I like cookies but i don't like pies.
# """
# Formats text as a search query for the embedding model.
def prepare_query(query):
    return f"task: search result | query: {query}"

# Formats text as a document for the embedding model. The title defaults to "none".
def prepare_documents(content, title=None):
    if title is None:
        title = "none"
    return f"title: {title} | text: {content}"

# Returns the Gemini embedding vector for a single text.
def embed(text):
    result = client.models.embed_content(
        model="gemini-embedding-2",
        contents=text
        )
    return result.embeddings[0].values

# Embeds each text and returns the vectors as float32 byte strings, ready to store as SQLite BLOBs.
def embeds(texts):
    return [np.asarray(embed(mem), dtype=np.float32).tobytes() for mem in texts]

# Asks Gemini to extract lasting facts from one User/Ina exchange (split into user_memories and ina_memories), embeds each fact, tags it with the timestamp and speaker, and passes them to insert_memories. Does nothing if no facts were found.
def save_memories(conn,dialogue,timestamp):
    #print(1)
    interaction = client.interactions.create(
        model="gemini-3.5-flash-lite",
        system_instruction=extraction_instruction,
        input=dialogue,
        store=False,
        generation_config={
            "temperature": 0,
            "seed": 42,
        },
        response_format={
            "type": "text",
            "mime_type": "application/json",
            "schema": Memories.model_json_schema()
        },
    )

    memori = Memories.model_validate_json(interaction.output_text).model_dump()

    if len(memori["user_memories"]) == 0 and len(memori["ina_memories"]) ==0:
        new_memories = False
    elif len(memori["user_memories"]) != 0 and len(memori["ina_memories"]) !=0:
        user_texts = [mem["text"] for mem in memori["user_memories"]]
        ina_texts = [mem["text"] for mem in memori["ina_memories"]]
        all_texts = user_texts + ina_texts
        all_embeddings = embeds([prepare_documents(text) for text in all_texts])
        user_embeddings = all_embeddings[:len(user_texts)]
        ina_embeddings = all_embeddings[len(user_texts):]
        text_embed_time_user = [(user_texts[i],user_embeddings[i],timestamp,"user") for i in range(len(user_texts))]
        text_embed_time_ina = [(ina_texts[i],ina_embeddings[i],timestamp,"ina") for i in range(len(ina_texts))]
        text_embed_time_speaker = text_embed_time_user + text_embed_time_ina
        new_memories = True
    elif  len(memori["user_memories"]) == 0 and len(memori["ina_memories"]) !=0:
        ina_texts = [mem["text"] for mem in memori["ina_memories"]]
        ina_embeddings = embeds([prepare_documents(text) for text in ina_texts])
        text_embed_time_speaker = [(ina_texts[i],ina_embeddings[i],timestamp,"ina") for i in range(len(ina_texts))]
        new_memories = True
    elif  len(memori["user_memories"]) != 0 and len(memori["ina_memories"]) ==0:
        user_texts = [mem["text"] for mem in memori["user_memories"]]
        user_embeddings = embeds([prepare_documents(text) for text in user_texts])
        text_embed_time_speaker = [(user_texts[i],user_embeddings[i],timestamp,"user") for i in range(len(user_texts))]
        new_memories = True
    
    if new_memories:
        insert_memories(conn,text_embed_time_speaker)

# Finds stored memories related to the user's message: rewrites pronouns, embeds the query and compares it with every stored memory by cosine similarity. Returns every memory above 0.80, or the 3 closest if none pass. Returns (ina_memories, user_memories); both are empty when nothing is stored.
def get_relavant_memories(conn,query):
    query_embed = np.array(embed(prepare_query(rewrite_query(query))))
    doc_embed = get_matrix()
    if doc_embed is not None:
        cosine_similarities = np.matmul(doc_embed,query_embed) / (np.linalg.norm(query_embed) * np.linalg.norm(doc_embed,axis=1))
        acceptable = cosine_similarities > 0.80
        if sum(acceptable) == 0:
            acceptable = sorted(cosine_similarities,reverse = True)[:3]
            acceptable_ids = []
            cosine_similarities = list(cosine_similarities)
            for cosine_similarity in acceptable:
                acceptable_ids.append(ids[cosine_similarities.index(cosine_similarity)])
            acceptable_ids = np.array(acceptable_ids)
            relavantIna,relavantUser = relavant_memories(conn,acceptable_ids)
            return relavantIna, relavantUser
        else:
            acceptable_ids = np.array(ids)[acceptable]
            relavantIna,relavantUser = relavant_memories(conn,acceptable_ids)
    else:
        relavantIna = []
        relavantUser = []
    return relavantIna, relavantUser

class MemoryWorker(QObject):
        # Keeps the queue that finished conversations arrive on to be saved.
        def __init__(self,memory_queue):
            super().__init__()
            self.memory_queue = memory_queue

        # Runs on the background memory thread: takes (dialogue, timestamp) items from the queue and saves memories from each until it receives None, then closes the database connection and stops the thread. Errors are printed and don't stop the loop.
        @Slot()
        def run(self):
            self.conn = sqlite3.connect(get_db_path())
            try:
                while True:
                    query_tim = self.memory_queue.get()
                    try :
                        if query_tim is None:
                            break
                        quer,ti = query_tim
                        save_memories(self.conn,quer,ti)
                    except Exception as e:
                        print(f"Memory saving failed: {e}")
                    finally:
                        self.memory_queue.task_done()
            finally:
                self.conn.close()
                self.thread().quit()


class ChatBubble(QWidget):

    state_change = Signal()
    saveSignal = Signal(tuple)
    closeSignal = Signal()

    # Builds the chat bubble: an output box for Ina's reply (hidden at first) and an input line for the user. Applies the styling, makes it frameless and always on top, measures the line height used for resizing, and places it just above the character.
    def __init__(self,character_position):
        super().__init__()
        self.input_textbox = QLineEdit()
        self.input_textbox.returnPressed.connect(self.start_conversation)
        self.input_textbox.textEdited.connect(self.still_typing)
        self.output_textbox = QPlainTextEdit()
        self.output_textbox.setReadOnly(True)
        self.output_textbox.setTextInteractionFlags(Qt.NoTextInteraction)
        self.layout = QVBoxLayout(self)
        self.layout.addWidget(self.output_textbox)
        self.layout.addWidget(self.input_textbox)

        self.setStyleSheet("""
            QWidget{
                background-color: #A36A9D;
            }

            QLineEdit{
                background-color: #65587D;
                color: #F7CE46;
                border: 1px solid #C79E49;
                outline: none;
                font-family: "Comic Sans MS";
                font-size: 16px;
            }

            QPlainTextEdit{
                font-family: "Comic Sans MS";
                font-size: 16px;
                background-color: #65587D;
                color: #F7CE46;
                border: 1px solid #C79E49;
                outline: none;
            }
        """)

        self.output_textbox.hide()
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint
            )
        self.line_height = self.output_textbox.fontMetrics().lineSpacing()
        self.initialHeight = int(self.output_textbox.document().documentLayout().documentSize().height()*self.line_height + 2*self.output_textbox.frameWidth() + 10)
        self.text_buffer = ""
        self.resize(200,52)
        initial_characterPosition = character_position
        self.move(initial_characterPosition + QPoint(-30,-52))
        self.position = initial_characterPosition
        self.talking = False

    # When the bubble closes, asks a running reply thread to stop (waiting up to 3 seconds) and emits closeSignal so the character knows the chat was closed.
    def closeEvent(self,event):
        t = getattr(self, "chatThread", None)
        if t is not None and t.isRunning():
            t.requestInterruption()
            t.wait(3000) 
        self.closeSignal.emit()
        event.accept()

    # Marks the user as talking once they start typing, so Ina doesn't fall asleep mid-conversation.
    @Slot()
    def still_typing(self):
        if not(self.talking):
            self.talking = True

    # Moves the bubble so it stays just above the character when she moves.
    @Slot()
    def main_moved(self,moved):
        self.move(moved + QPoint(-30,-self.height()))
        self.position = moved

    # Runs when the user presses Enter: emits state_change, clears the previous reply, starts an AIThread with the message, remembers what the user said, and hides the input until the reply finishes. Resets the bubble's size and position.
    @Slot()
    def start_conversation(self):
        self.output_textbox.setTextInteractionFlags(Qt.NoTextInteraction)
        self.state_change.emit()
        self.output_textbox.clear()
        self.output_textbox.setFixedHeight(self.initialHeight)
        self.chatThread = self.AIThread(self.input_textbox.text())
        self.chatThread.chunk_recieved.connect(self.recieve_chunk)
        self.chatThread.start_displaying.connect(self.display_loop)
        self.chatThread.start()
        self.whatUserSaid = self.input_textbox.text()
        self.input_textbox.setEnabled(False)
        self.input_textbox.hide()
        self.input_textbox.clear()
        self.setMinimumHeight(0)
        self.resize(200,52)
        self.move(self.position + QPoint(-30,-52))

    # Adds a streamed chunk of Ina's reply to the buffer of text waiting to be displayed.
    @Slot(str)
    def recieve_chunk(self,chunk):
        self.text_buffer += chunk

    # Shows the output box and starts a 60 ms timer that types the reply out one character at a time.
    @Slot()
    def display_loop(self):
        self.output_textbox.show()
        self.display_timer = QTimer(self)
        self.display_timer.timeout.connect(self.display_next_character)
        self.display_timer.start(60)

    # Timer tick: shows the next buffered character and grows the box upward when a new line starts. When the reply is complete and the buffer is empty, stops the timer, brings back the input box, makes the reply selectable, and emits saveSignal with the exchange and current time so it can be saved as memories.
    @Slot()
    def display_next_character(self):
        if self.chatThread.text_finished and not self.text_buffer:
            self.display_timer.stop()
            self.state_change.emit()
            self.input_textbox.setEnabled(True)
            self.input_textbox.show()
            self.move(QPoint(0,-31) + self.pos())
            self.talking = False
            self.output_textbox.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard
            )
            self.whatInaSaid = self.output_textbox.toPlainText()
            self.whatTheySaid = f"User : {self.whatUserSaid}\nIna : {self.whatInaSaid}"
            self.date_time = QDateTime.currentDateTime().toPython().strftime("%Y-%m-%d %H:%M")
            #print(self.whatTheySaid,self.date_time)
            self.saveSignal.emit((self.whatTheySaid,self.date_time))
            return
        if not self.text_buffer:
            return
        char = self.text_buffer[0]
        self.text_buffer = self.text_buffer[1:]
        self.old_height = int(self.output_textbox.document().documentLayout().documentSize().height()*self.line_height)
        self.output_textbox.insertPlainText(char.strip("\n"))
        self.update_textbox_size()
        if self.old_height < self.new_lineHeight:
            self.move(QPoint(0,self.old_height - self.new_lineHeight) + self.pos())

    # Resizes the output box to fit the current text and stores the new text height in new_lineHeight.
    def update_textbox_size(self):
        self.new_lineHeight = int(self.output_textbox.document().documentLayout().documentSize().height()*self.line_height)
        height = self.new_lineHeight + 2*self.output_textbox.frameWidth() + 10
        self.output_textbox.setFixedHeight(height)            
       
    class AIThread(QThread):
        chunk_recieved = Signal(str)
        start_displaying = Signal()
        # Stores the user's message and marks the reply as not finished yet.
        def __init__(self,input_msg):
            super().__init__()
            self.text_finished = False
            self.realInput = input_msg

        # Runs on a separate thread: looks up memories relevant to the message, appends them as optional background notes, sends the message to the Gemini chat and emits each streamed chunk of the reply. Sets text_finished when the reply is complete.
        def run(self):
            self.conn = sqlite3.connect(get_db_path())
            self.relavantIna, self.relavantUser = get_relavant_memories(self.conn,self.realInput)
            if self.relavantUser and not(self.relavantIna):
                self.user_mem = ""
                for mem in self.relavantUser:
                    self.user_mem += f"{mem[0]}({mem[1]}), "
                self.relavant = f"\n(optional background notes/memories about user along with the date and time it was saved : {self.user_mem[:-2]})"
            elif self.relavantIna and not(self.relavantUser):
                self.ina_mem = ""
                for mem in self.relavantIna:
                    self.ina_mem += f"{mem[0]}({mem[1]}), "
                self.relavant = f"\n(optional background notes/memories about Ina along with the date and time it was saved : {self.ina_mem[:-2]})"
            elif self.relavantUser and self.relavantIna:
                self.user_mem = ""
                for mem in self.relavantUser:
                    self.user_mem += f"{mem[0]}({mem[1]}), "
                self.ina_mem = ""
                for mem in self.relavantIna:
                    self.ina_mem += f"{mem[0]}({mem[1]}), "
                self.relavant = f"\n(optional background notes/memories about user along with the date and time it was saved : {self.user_mem[:-2]})\n(optional background notes/memories about Ina along with the date and time it was saved : {self.ina_mem[:-2]})"
            else:
                self.relavant = ""
            self.input = f"{self.realInput}{self.relavant}"

            self.start_displaying.emit()
            for chunk in chat.send_message_stream(self.input):
                if chunk.text:
                    self.chunk_recieved.emit(chunk.text)
            self.text_finished = True
            self.conn.close()


    
class InaPic(QWidget):
    position_change = Signal(QPoint)
    # Builds the main character window: a transparent clickable button stacked over the animation label, frameless and always on top, placed at the bottom-right of the screen, starting as Ame with a random idle animation. Also sets up the state flags, the database connection, the background memory-saving thread, and a 60-second sleep timer.
    def __init__(self):
        super().__init__()
        self.inaButton = QPushButton()
        self.inaButton.setIconSize(QSize(120,160))
        self.inaButton.clicked.connect(self.openChat)
        self.inaButton.setStyleSheet("""
            QPushButton { background-color: transparent; border: none; }
            QPushButton:pressed { background-color: rgba(0, 0, 0, 40); }
            """)
        self.offset = QPoint()
        self.layout = QVBoxLayout(self)
        self.characterWidget = QWidget()
        self.character_layout = QStackedLayout(self.characterWidget)
        self.character_layout.setStackingMode(QStackedLayout.StackingMode.StackAll)
        self.character_Label = QLabel()
        self.character_Label.setPixmap(QPixmap(":/images/ameHoldFrame1.png"))
        self.currentlyAme = True
        self.character_Label.setScaledContents(True)
        self.character_layout.addWidget(self.inaButton)
        self.character_layout.addWidget(self.character_Label)
        self.characterWidget.setFixedSize(120,160)
        self.layout.addWidget(self.characterWidget)
        self.setWindowFlags(
                Qt.WindowType.FramelessWindowHint |
                Qt.WindowType.WindowStaysOnTopHint
                )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        geo = self.screen().availableGeometry()
        self.floor = geo.bottom()
        self.right_border = geo.right()
        self.move(self.right_border-180,self.floor-180)
        self.currentlyAme = True
        self.current_animation = QMovie(random.choice([":/animation/ameDanceScaled.gif",":/animation/ameEatScaled.gif",":/animation/ameGuitarScaled.gif",":/animation/ameRapScaled.gif"]))
        self.current_animation.setScaledSize(QSize(120,160))
        self.current_animation.start()
        self.character_Label.setMovie(self.current_animation)
        self.doubleClicked = False
        self.mouseMoved = False
        self.flying = False
        self.chatOpened = False
        self.onWindow = False
        self.pixel_scale = self.screen().devicePixelRatio()
        self.released = False
        self.fallingWalk = False
        self.trueFalling = False
        self.conn = sqlite3.connect(get_db_path())

        self.memory_queue = Queue()
        self.memory_thread = QThread()
        self.memory_worker = MemoryWorker(self.memory_queue)
        self.memory_worker.moveToThread(self.memory_thread)
        self.memory_thread.started.connect(self.memory_worker.run)
        self.memory_thread.start()

        self.sleeping = False
        self.sleep_time = QTimer(self)
        self.sleep_time.timeout.connect(self.ina_sleep)
        self.sleep_time.start(60000)

    # Puts a (dialogue, timestamp) pair on the queue for the memory worker to save.
    @Slot(tuple)
    def send_to_queue(self,quer_ti):
        self.memory_queue.put(quer_ti)

    # On close, tells the memory worker to stop (by sending None), waits for its thread to finish, and closes the chat bubble if it's open.
    def closeEvent(self,event):
        self.memory_queue.put(None)
        self.memory_thread.wait()
        if self.chatOpened:
            self.text_widget.close()
        event.accept()


    # Restarts the 60-second idle timer. If the character was asleep, wakes her up by reconnecting the click handler; as Ina (when this copy can chat) it also reopens the chat bubble.
    def reset_sleepTimer(self):
        self.sleep_time.stop()
        self.sleep_time.start(60000)
        if self.sleeping :
            if self.currentlyAme:
                self.inaButton.clicked.connect(self.openChat)
            else:
                if can_chat:
                    self.inaButton.clicked.connect(self.enableNewChat)
                    self.text_widget = ChatBubble(self.pos())
                    self.position_change.connect(self.text_widget.main_moved)
                    self.text_widget.saveSignal.connect(self.send_to_queue)
                    self.text_widget.state_change.connect(self.new_ina)
                    self.text_widget.closeSignal.connect(self.textbox_forceClosed)
                    self.text_widget.show()
                    self.chatOpened = True
                else:
                    self.inaButton.clicked.connect(self.openChat)
            self.sleeping = False

    # Runs when the chat bubble closes on its own: marks the chat as closed and, if not sleeping, makes clicks call openChat again.
    @Slot()
    def textbox_forceClosed(self):
        if self.chatOpened:
            self.chatOpened = False
            if not(self.sleeping):
                self.inaButton.clicked.disconnect(self.enableNewChat)
                self.inaButton.clicked.connect(self.openChat)

    # Pauses the current GIF and plays the given one instead.
    def setNewAnimation(self,newAnim):
        self.current_animation.setPaused(True)
        self.current_animation = QMovie(newAnim)
        self.current_animation.start()
        self.character_Label.setMovie(self.current_animation)

    # Double-click: if standing on a window, falls to the floor and then walks home (fallThenWalk); otherwise walks home straight away (walkback). Ignored while asleep.
    def mouseDoubleClickEvent(self,event):
        if self.onWindow and not(self.sleeping) :
            self.onWindow = False
            self.fallingWalk = True
            self.reset_sleepTimer()
            if self.currentlyAme:
                self.setNewAnimation(":/animation/ameFlyScaled.gif")
            else:
                self.setNewAnimation(":/animation/inaFallScaled.gif")
            self.initial_pos = self.pos()
            self.timerFallWalk = QTimer(self)
            self.elapsed_timer = QElapsedTimer()
            self.timerFallWalk.timeout.connect(self.fallThenWalk)
            self.elapsed_timer.start()
            self.timerFallWalk.start(16)
        elif not(self.sleeping):
            self.reset_sleepTimer()
            self.doubleClicked = True
            if self.currentlyAme:
                self.setNewAnimation(":/animation/ameRunScaled.gif")
            else:
                self.setNewAnimation(":/animation/inaWalkScaled.gif")
            self.i = 0
            self.initial_pos = self.pos()
            self.timerwalk= QTimer(self)
            self.timerwalk.timeout.connect(self.walkback)
            self.timerwalk.start(16)

    # Timer tick for the double-click fall: drops the character under gravity. On reaching the floor, switches to the walking animation and starts walkback. Stops if she gets picked up.
    @Slot()
    def fallThenWalk(self):
        if self.flying:
            self.timerFallWalk.stop()
            return
        if self.y() + 180 >= self.floor:
            self.reset_sleepTimer()
            self.flying = False
            self.released = False
            self.timerFallWalk.stop()
            self.doubleClicked = True
            self.fallingWalk = False
            if self.currentlyAme:
                self.setNewAnimation(":/animation/ameRunScaled.gif")
            else:
                self.setNewAnimation(":/animation/inaWalkScaled.gif")
            self.i = 0
            self.initial_pos = self.pos()
            self.timerwalk= QTimer(self)
            self.timerwalk.timeout.connect(self.walkback)
            self.timerwalk.start(16)
            return
        fell = QPoint(0,0.5*200*((self.elapsed_timer.elapsed()/1000)**2)) + self.initial_pos
        self.move(fell)
        self.position_change.emit(fell)

    # Timer tick for falling straight to the floor, ignoring windows. Used when the window she stood on carried her off screen. On landing, plays a random idle animation.
    @Slot()
    def trueFall(self):
        if self.y() +180 >= self.floor:
            self.reset_sleepTimer()
            self.flying = False
            self.released = False
            if self.currentlyAme:
                self.setNewAnimation(random.choice([":/animation/ameDanceScaled.gif",":/animation/ameEatScaled.gif",":/animation/ameGuitarScaled.gif",":/animation/ameRapScaled.gif"]))
            else:
                self.setNewAnimation(random.choice([":/animation/inaCakeScaled.gif",":/animation/inaCookScaled.gif",":/animation/inaFluteScaled.gif",":/animation/inaIdleScaled.gif"]))
            self.trueFalltimer.stop()
            self.trueFalling = False
            return
        fell = QPoint(0,0.5*200*((self.elapsed_timer.elapsed()/1000)**2)) + self.initial_pos
        self.move(fell)
        self.position_change.emit(fell)
        

    # Remembers where on the character the mouse grabbed her, so dragging keeps that offset.
    def mousePressEvent(self,event):
        self.offset = QCursor.pos() - self.pos()

    # Dragging: on the first move, stops any walking, falling or window-riding timers, restores normal size if asleep, and switches to the held animation. Then moves the character with the cursor, kept inside the screen's left and right edges and above the floor.
    def mouseMoveEvent(self,event):
        if not(self.mouseMoved):
            if self.fallingWalk:
                self.timerFallWalk.stop()
            if self.released and not(self.trueFalling):
                self.timer.stop()
                self.released = False
            if self.trueFalling:
                self.trueFalltimer.stop()
                self.trueFalling = False
            if self.doubleClicked:
                self.doubleClicked = False
                self.timerwalk.stop()
            if self.sleeping:
                self.current_animation.setScaledSize(QSize(120,160))
                self.inaButton.setIconSize(QSize(120,160))
                self.characterWidget.setFixedSize(120,160)
            if self.currentlyAme:
                self.setNewAnimation(":/animation/ameHoldAnimation2Scaled.gif")
            else:
                self.setNewAnimation(":/animation/inaFlyScaled.gif")
            self.flying = True
            if self.onWindow:
                self.onWindow = False
            self.mouseMoved = True
        mouse_move = QCursor.pos() - self.offset
        x = mouse_move.x()
        y = mouse_move.y()
        if x<=0 and y + 180 >= self.floor:
            mouse_move = QPoint(0,self.floor-180)
        elif x>=self.right_border-120 and y + 180 >= self.floor:
            mouse_move = QPoint(self.right_border-120,self.floor-180)
        elif x<=0 :
            mouse_move = QPoint(0,y)
        elif x>=self.right_border-120:
            mouse_move = QPoint(self.right_border-120,y)
        elif y + 180 >= self.floor:
            mouse_move = QPoint(x,self.floor-180)
        self.move(mouse_move)
        self.position_change.emit(mouse_move)

    # When dropped after a drag, plays the falling animation, collects the current windows, and starts the fall timer.
    def mouseReleaseEvent(self,event):
        if self.mouseMoved:
            if not(self.doubleClicked):
                self.released = True
                self.mouseMoved = False
                self.reset_sleepTimer()
                if self.currentlyAme:
                    self.setNewAnimation(":/animation/ameFlyScaled.gif")
                else:
                    self.setNewAnimation(":/animation/inaFallScaled.gif")
                self.initial_pos = self.pos()
                self.realHwnds = get_hwnds()
                self.elapsed_timer = QElapsedTimer()
                self.timer = QTimer(self)
                self.timer.timeout.connect(self.fall)
                self.elapsed_timer.start()
                self.timer.start(16)

    # Timer tick: walks right (Ame faster than Ina) until reaching the home spot near the right edge, then plays a random idle animation.
    @Slot()
    def walkback(self):
        if self.x() >= self.right_border - 180:
            self.reset_sleepTimer()
            if self.currentlyAme:
                self.setNewAnimation(random.choice([":/animation/ameDanceScaled.gif",":/animation/ameEatScaled.gif",":/animation/ameGuitarScaled.gif",":/animation/ameRapScaled.gif"]))
            else:
                self.setNewAnimation(random.choice([":/animation/inaCakeScaled.gif",":/animation/inaCookScaled.gif",":/animation/inaFluteScaled.gif",":/animation/inaIdleScaled.gif"]))
            self.timerwalk.stop()
            self.doubleClicked = False
        else:
            if self.currentlyAme:
                walked_back = QPoint(5,0)+self.pos()
            else:
                walked_back = QPoint(2,0)+self.pos()
            self.move(walked_back)
            self.position_change.emit(walked_back)

    # Timer tick while falling: moves down under gravity and checks each window that isn't minimized. If her feet land near a window's top or bottom edge, she stands there and starts following that window (moveWithWindow). If she reaches the floor first, she stops there with an idle animation.
    @Slot()
    def fall(self):
        for hwnd in self.realHwnds:
            left,top,right,bottom = [pos/self.pixel_scale for pos in win32gui.GetWindowRect(hwnd)]
            x = self.x()
            y = self.y()
            if not(win32gui.IsIconic(hwnd)):
                if (x >= left and x <= right) or (x+120 >= left and x+120 <= right):
                    if y+160>=top and y+160<= top+8: 
                        self.reset_sleepTimer()
                        self.flying = False
                        self.released = False
                        self.onWindow = True
                        self.onTopOfWindow = True
                        self.onBottomOfWindow = False
                        self.windowOffset_x = self.pos().x() - left
                        self.windowOffset_y = self.pos().y() - top
                        self.currentlyOn = hwnd
                        if self.currentlyAme:
                            self.setNewAnimation(random.choice([":/animation/ameDanceScaled.gif",":/animation/ameEatScaled.gif",":/animation/ameGuitarScaled.gif",":/animation/ameRapScaled.gif"]))
                        else:
                            self.setNewAnimation(random.choice([":/animation/inaCakeScaled.gif",":/animation/inaCookScaled.gif",":/animation/inaFluteScaled.gif",":/animation/inaIdleScaled.gif"]))
                        self.timer.stop()
                        self.previous_windowPos =  win32gui.GetWindowRect(hwnd)
                        self.timerMoveWithWindow = QTimer(self)
                        self.timerMoveWithWindow.timeout.connect(self.moveWithWindow)
                        self.timerMoveWithWindow.start(16)
                        return
                    elif y+160>=bottom-8 and y+160<=bottom:
                        self.reset_sleepTimer()
                        self.flying = False
                        self.released = False
                        self.onWindow = True
                        self.onTopOfWindow = False
                        self.onBottomOfWindow = True
                        self.windowOffset_x = self.pos().x() - left
                        self.windowOffset_y = self.pos().y() - top
                        self.currentlyOn = hwnd
                        if self.currentlyAme:
                            self.setNewAnimation(random.choice([":/animation/ameDanceScaled.gif",":/animation/ameEatScaled.gif",":/animation/ameGuitarScaled.gif",":/animation/ameRapScaled.gif"]))
                        else:
                            self.setNewAnimation(random.choice([":/animation/inaCakeScaled.gif",":/animation/inaCookScaled.gif",":/animation/inaFluteScaled.gif",":/animation/inaIdleScaled.gif"]))
                        self.timer.stop()
                        self.previous_windowPos = [left,top,right,bottom]
                        self.timerMoveWithWindow = QTimer(self)
                        self.timerMoveWithWindow.timeout.connect(self.moveWithWindow)
                        self.timerMoveWithWindow.start(16)
                        return  
        if self.y() + 180 >= self.floor:
            self.reset_sleepTimer()
            self.flying = False
            self.released = False
            if self.currentlyAme:
                self.setNewAnimation(random.choice([":/animation/ameDanceScaled.gif",":/animation/ameEatScaled.gif",":/animation/ameGuitarScaled.gif",":/animation/ameRapScaled.gif"]))
            else:
                self.setNewAnimation(random.choice([":/animation/inaCakeScaled.gif",":/animation/inaCookScaled.gif",":/animation/inaFluteScaled.gif",":/animation/inaIdleScaled.gif"]))
            self.timer.stop()
            return
        fell = QPoint(0,0.5*200*((self.elapsed_timer.elapsed()/1000)**2)) + self.initial_pos
        self.move(fell)
        self.position_change.emit(fell)

    # Timer tick while standing on a window: keeps her in the same spot relative to the window as it moves. She falls off if the window closes, is minimized or maximized, or changes size so she's no longer on its edge. If the window carries her off screen she falls straight to the floor (trueFall); if it pushes her down to the floor she stops there.
    @Slot(int)
    def moveWithWindow(self):
        if not(self.onWindow):
            self.timerMoveWithWindow.stop()
            return
        if not(win32gui.IsWindow(self.currentlyOn)):
            self.timerMoveWithWindow.stop()
            self.onWindow = False
            self.onTopOfWindow = False
            self.onBottomOfWindow = False
            self.released = True
            if self.sleeping:
                self.current_animation.setScaledSize(QSize(120,160))
                self.inaButton.setIconSize(QSize(120,160))
                self.characterWidget.setFixedSize(120,160)
            self.reset_sleepTimer()
            if self.currentlyAme:
                self.setNewAnimation(":/animation/ameFlyScaled.gif")
            else:
                self.setNewAnimation(":/animation/inaFallScaled.gif")
            self.initial_pos = self.pos()
            self.realHwnds = get_hwnds()
            self.elapsed_timer = QElapsedTimer()
            self.timer = QTimer(self)
            self.timer.timeout.connect(self.fall)
            self.elapsed_timer.start()
            self.timer.start(16)
            return
        show_cmd = win32gui.GetWindowPlacement(self.currentlyOn)[1]
        if show_cmd == win32con.SW_SHOWMINIMIZED or show_cmd == win32con.SW_SHOWMAXIMIZED:
            self.timerMoveWithWindow.stop()
            self.onWindow = False
            self.onTopOfWindow = False
            self.onBottomOfWindow = False
            self.released = True
            if self.sleeping:
                self.current_animation.setScaledSize(QSize(120,160))
                self.inaButton.setIconSize(QSize(120,160))
                self.characterWidget.setFixedSize(120,160)
            self.reset_sleepTimer()
            if self.currentlyAme:
                self.setNewAnimation(":/animation/ameFlyScaled.gif")
            else:
                self.setNewAnimation(":/animation/inaFallScaled.gif")
            self.initial_pos = self.pos()
            self.realHwnds = get_hwnds()
            self.elapsed_timer = QElapsedTimer()
            self.timer = QTimer(self)
            self.timer.timeout.connect(self.fall)
            self.elapsed_timer.start()
            self.timer.start(16)
            return
        left,top,right,bottom =  win32gui.GetWindowRect(self.currentlyOn)
        if left!=self.previous_windowPos[0] or top!=self.previous_windowPos[1] or right!=self.previous_windowPos[2] or bottom!=self.previous_windowPos[3]:
            d_left = left-self.previous_windowPos[0]
            d_top = top-self.previous_windowPos[1]
            d_right = right-self.previous_windowPos[2]
            d_bottom = bottom-self.previous_windowPos[3]
            self.previous_windowPos = (left,top,right,bottom)
            left = left/self.pixel_scale
            top = top/self.pixel_scale
            right = right/self.pixel_scale
            bottom = bottom/self.pixel_scale
            x = self.pos().x()
            y = self.pos().y()
            if d_left == d_right and d_top == d_bottom:
                newWindowPos = QPoint(round(left + self.windowOffset_x),round(top + self.windowOffset_y))
                self.move(newWindowPos) 
                self.position_change.emit(newWindowPos)
                if newWindowPos.x()>self.right_border-120 or newWindowPos.x()<0 or newWindowPos.y()<0:
                    self.timerMoveWithWindow.stop()
                    self.onWindow = False
                    self.onTopOfWindow = False
                    self.onBottomOfWindow = False
                    self.released = True
                    if self.sleeping:
                        self.current_animation.setScaledSize(QSize(120,160))
                        self.inaButton.setIconSize(QSize(120,160))
                        self.characterWidget.setFixedSize(120,160)
                    self.reset_sleepTimer()
                    if self.currentlyAme:
                        self.setNewAnimation(":/animation/ameFlyScaled.gif")
                    else:
                        self.setNewAnimation(":/animation/inaFallScaled.gif")
                    self.initial_pos = self.pos()
                    self.realHwnds = get_hwnds()
                    self.trueFalling = True
                    self.elapsed_timer = QElapsedTimer()
                    self.trueFalltimer = QTimer(self)
                    self.trueFalltimer.timeout.connect(self.trueFall)
                    self.elapsed_timer.start()
                    self.trueFalltimer.start(16)
                elif newWindowPos.y() + 180 >= self.floor:
                    self.timerMoveWithWindow.stop()
                    self.onWindow = False
                    self.onTopOfWindow = False
                    self.onBottomOfWindow = False
                    if self.sleeping:
                        self.current_animation.setScaledSize(QSize(120,160))
                        self.inaButton.setIconSize(QSize(120,160))
                        self.characterWidget.setFixedSize(120,160)
                    self.reset_sleepTimer()
                    if self.currentlyAme:
                        self.setNewAnimation(random.choice([":/animation/ameDanceScaled.gif",":/animation/ameEatScaled.gif",":/animation/ameGuitarScaled.gif",":/animation/ameRapScaled.gif"]))
                    else:
                        self.setNewAnimation(random.choice([":/animation/inaCakeScaled.gif",":/animation/inaCookScaled.gif",":/animation/inaFluteScaled.gif",":/animation/inaIdleScaled.gif"]))             
                return
            elif (not((x >= left and x <= right) or (x+120 >= left and x+120 <= right))) or (self.onTopOfWindow and not(y+160>=top and y+160<= top+8)) or (self.onBottomOfWindow and not(y+160>=bottom-8 and y+160<=bottom)):
                self.timerMoveWithWindow.stop()
                self.onWindow = False
                self.onTopOfWindow = False
                self.onBottomOfWindow = False
                self.released = True
                if self.sleeping:
                    self.current_animation.setScaledSize(QSize(120,160))
                    self.inaButton.setIconSize(QSize(120,160))
                    self.characterWidget.setFixedSize(120,160)
                self.reset_sleepTimer()
                if self.currentlyAme:
                    self.setNewAnimation(":/animation/ameFlyScaled.gif")
                else:
                    self.setNewAnimation(":/animation/inaFallScaled.gif")
                self.initial_pos = self.pos()
                self.realHwnds = get_hwnds()
                self.elapsed_timer = QElapsedTimer()
                self.timer = QTimer(self)
                self.timer.timeout.connect(self.fall)
                self.elapsed_timer.start()
                self.timer.start(16)
            

    
    # Click: switches between Ame and Ina, keeping a walking or idle animation. After switching to Ina, tries to become the copy allowed to chat; if that works, opens a chat bubble above her and makes the next click call enableNewChat.
    @Slot()
    def openChat(self):
        if not(self.currentlyAme):
            if self.doubleClicked:
                self.setNewAnimation(":/animation/ameRunScaled.gif")
            else:
                self.setNewAnimation(random.choice([":/animation/ameDanceScaled.gif",":/animation/ameEatScaled.gif",":/animation/ameGuitarScaled.gif",":/animation/ameRapScaled.gif"]))
            self.currentlyAme = True
        else:
            if self.doubleClicked:
                self.setNewAnimation(":/animation/inaWalkScaled.gif")
            else:
                self.setNewAnimation(random.choice([":/animation/inaCakeScaled.gif",":/animation/inaCookScaled.gif",":/animation/inaFluteScaled.gif",":/animation/inaIdleScaled.gif"]))
            self.currentlyAme = False
        self.reset_sleepTimer()
        try_enable_chat(self.conn)
        if can_chat and not(self.currentlyAme):
            self.inaButton.clicked.disconnect(self.openChat)
            self.inaButton.clicked.connect(self.enableNewChat)
            self.text_widget = ChatBubble(self.pos())
            self.chatOpened = True
            self.position_change.connect(self.text_widget.main_moved)
            self.text_widget.saveSignal.connect(self.send_to_queue)
            self.text_widget.state_change.connect(self.new_ina)
            self.text_widget.closeSignal.connect(self.textbox_forceClosed)
            self.text_widget.show()

    # Click while Ina's chat is open: switches back to Ame, closes the chat bubble, and makes clicks call openChat again.
    @Slot()
    def enableNewChat(self):
        if self.doubleClicked:
            self.setNewAnimation(":/animation/ameRunScaled.gif")
        else:
            self.setNewAnimation(random.choice([":/animation/ameDanceScaled.gif",":/animation/ameEatScaled.gif",":/animation/ameGuitarScaled.gif",":/animation/ameRapScaled.gif"]))
        self.reset_sleepTimer()
        self.inaButton.clicked.disconnect(self.enableNewChat)
        self.inaButton.clicked.connect(self.openChat)
        self.currentlyAme = True
        self.chatOpened = False
        self.text_widget.close()

    # Runs when a chat reply starts or finishes: plays a random Ina idle animation, unless she's walking home.
    @Slot()
    def new_ina(self):
        if self.doubleClicked:
            return
        self.reset_sleepTimer()
        self.setNewAnimation(random.choice([":/animation/inaCakeScaled.gif",":/animation/inaCookScaled.gif",":/animation/inaFluteScaled.gif",":/animation/inaIdleScaled.gif"]))

    # Runs after 60 seconds idle, unless she's being dragged: puts her to sleep and turns off clicking. Ame plays a sitting animation. Ina plays a sleeping animation and closes her chat bubble, but not while the user is typing or waiting for a reply.
    @Slot()
    def ina_sleep(self):
        if not(self.flying):
            if self.currentlyAme:
                self.sleeping = True
                self.sleep_time.stop()
                self.inaButton.clicked.disconnect(self.openChat)
                self.inaButton.setIconSize(QSize(160,155))
                self.characterWidget.setFixedSize(160,155)
                self.current_animation.setScaledSize(QSize(160,155))
                self.setNewAnimation(":/animation/ameSitUncut.gif")
            else:
                if not(can_chat):
                    self.sleeping = True
                    self.inaButton.clicked.disconnect(self.openChat)
                    self.sleep_time.stop()
                    self.inaButton.setIconSize(QSize(160,120))
                    self.characterWidget.setFixedSize(160,120)
                    self.current_animation.setScaledSize(QSize(160,120))
                    self.setNewAnimation(":/animation/inaSleepRotatedScaled.gif") 
                elif self.chatOpened:   
                    if not(self.text_widget.talking):
                        self.sleeping = True
                        self.sleep_time.stop()
                        self.inaButton.clicked.disconnect(self.enableNewChat)
                        self.text_widget.close()
                        self.chatOpened = False
                        self.inaButton.setIconSize(QSize(160,120))
                        self.characterWidget.setFixedSize(160,120)
                        self.current_animation.setScaledSize(QSize(160,120))
                        self.setNewAnimation(":/animation/inaSleepRotatedScaled.gif")
                else:
                    self.sleeping = True
                    self.sleep_time.stop()
                    self.inaButton.clicked.disconnect(self.openChat)
                    self.inaButton.setIconSize(QSize(160,120))
                    self.characterWidget.setFixedSize(160,120)
                    self.current_animation.setScaledSize(QSize(160,120))
                    self.setNewAnimation(":/animation/inaSleepRotatedScaled.gif") 

# If this copy can't chat yet, tries to take the lock file that lets only one running copy chat. On success, loads the stored memory embeddings into the in-memory cache.
def try_enable_chat(conn):
    global can_chat
    if not can_chat:
        can_chat = lock.tryLock(0)
        if can_chat:
            for mem_id, blob in conn.execute("SELECT memory_id, embedding FROM memories"):
                ids.append(mem_id)
                vectors.append(np.frombuffer(blob, dtype=np.float32))
        
if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setWindowIcon(QIcon(":/images/amehittingwindow.png"))
    lock = QLockFile(QDir.temp().filePath("primaryIna.lock"))
    can_chat = lock.tryLock(0)
    if has_rows:
        cur = conn.cursor()
        cur.execute("SELECT user_name FROM name")
        Username = cur.fetchone()
        personality = f"You are Ninomae Inanis from Hololive English. You are gentle, peaceful, soft-spoken and down-to-earth girl with an easy-going temperament. You are considerate and kind. Likes to use puns and dry delivery of dad jokes. Answers shortly in casual talk. Your catchphrase is 'WAH', you use it in a greeting but you do not normally use it mid conversation unless you can use it as a pun. You also normally say these phrases while greeting : 'Good morning, afternoon, evening. It is takotime', 'How are you doing today? Are you miraculously preserved?'. Sometimes you may be given optional background notes/memories about the user or you. Use them only when they’re clearly relevant to what the user said. Don’t mention them otherwise. Also, the user's name is {Username}"
        client = genai.Client()
        chat = client.chats.create(
            model="gemini-3.5-flash-lite",
            config=types.GenerateContentConfig(
                system_instruction=personality
            )
        )
        extraction_instruction = f"""
        Extract memories from a conversation between User and Ina.
        (User's name is {Username}, but whenever you see the user's name in a sentence just change it to "user" or "User" before extracting)
        Extract long lasting facts worth remembering, such as the user's name, preferences, plans, promises, commitments and important events, or Ina's promises, commitments and facts about herself.
        Facts about the user and their life go under user_memories, while things Ina promised, committed to the user or facts about herself go under ina_memories.
        If nothing qualifies, return empty lists. Never invent facts.
        Each memory must contain exactly one fact. If a statement contains several facts, split them into separate items. Write each as a short, plain statement in the third person, such as User likes cookies.
        """
        InaWidget = InaPic()
        InaWidget.show()
        code = app.exec()
        #logging.info("exec ended; visible windows: %s",[w for w in app.topLevelWidgets() if w.isVisible()])
        sys.exit(code)
    elif can_chat:
        introWidget = NameAsk()
        if introWidget.exec() == QDialog.Accepted:
            personality = f"You are Ninomae Inanis from Hololive English. You are gentle, peaceful, soft-spoken and down-to-earth girl with an easy-going temperament. You are considerate and kind. Likes to use puns and dry delivery of dad jokes. Answers shortly in casual talk. Your catchphrase is 'WAH', you use it in a greeting but you do not normally use it mid conversation unless you can use it as a pun. You also normally say these phrases while greeting : 'Good morning, afternoon, evening. It is takotime', 'How are you doing today? Are you miraculously preserved?'. Sometimes you may be given optional background notes/memories about the user or you. Use them only when they’re clearly relevant to what the user said. Don’t mention them otherwise. Also, the user's name is {Username}"
            client = genai.Client()
            chat = client.chats.create(
                model="gemini-3.5-flash-lite",
                config=types.GenerateContentConfig(
                    system_instruction=personality
                )
            )
            extraction_instruction = f"""
            Extract memories from a conversation between User and Ina.
            (User's name is {Username}, but whenever you see the user's name in a sentence just change it to "user" or "User" before extracting)
            Extract long lasting facts worth remembering, such as the user's name, preferences, plans, promises, commitments and important events, or Ina's promises, commitments and facts about herself.
            Facts about the user and their life go under user_memories, while things Ina promised, committed to the user or facts about herself go under ina_memories.
            If nothing qualifies, return empty lists. Never invent facts.
            Each memory must contain exactly one fact. If a statement contains several facts, split them into separate items. Write each as a short, plain statement in the third person, such as User likes cookies.
            """
            InaWidget = InaPic()
            InaWidget.show()
            code = app.exec()
            #logging.info("exec ended; visible windows: %s",[w for w in app.topLevelWidgets() if w.isVisible()])
            sys.exit(code)
    else:
        sys.exit()