"""The participation agreement shown to a person who joins from outside the home network.
DRAFT written by the developer in plain language. It is NOT legal advice: the owner of the Mesh must review it (and ideally have it
reviewed) before it is sent to anyone."""

CONSENT_TEXT = ("I agree that Echo may use my computer’s approved AI resources for authorized coding, research and design tasks "
                "in exchange for reciprocal access to the Echo Mesh resource pool.")
READ_TEXT = "I have read and understood the agreement above."

AGREEMENT = """ECHO MESH PARTICIPATION AGREEMENT

1. What this is
Echo Mesh is a private network of computers that share spare computing time with each other. You are being invited to add your computer. The software on your computer is a small program, "Echo Mesh", that connects to the network owner's coordinator and runs model requests you allow. Both sides give and both sides receive.

2. What your computer will do
It will run only model requests (asking an AI a question and returning the answer) for jobs the network owner chooses to send. It never runs commands, scripts or programs sent over the network, never reads your files, and never installs anything else.

3. What you receive
Your AI may ask the Mesh to run its requests on the network owner's computers. You can use at most 300 seconds of work more than your computer has given (this allowance may be adjusted with your agreement). Work is counted in seconds of real work; a fast and a slow computer are not yet counted differently.

4. Your limits (you stay in charge)
- Your computer shares at most 30% of its time. This is a hard limit enforced by the program on your computer, not just a setting on a screen.
- Your own work always comes first: the program takes no shared work while you are using your computer for your own tasks (where Windows lets it measure this).
- You can pause sharing from the tray icon at any time, change the limit downward, or remove the program completely.
- The network owner can also pause or remove your computer. Either of us may end this agreement at any time, for any reason, without notice.

5. What is private
Your AI keeps its own identity, memory, personality and files. The Mesh never asks for them and your AI never needs to give them. The network owner's AI keeps its private information as well: its private prompts, memories, trained adapters and recordings are never sent to your computer.
Only the text of a job travels: when your computer runs a job, your AI client sees that job's prompt, and the network owner's computers see the prompts of the requests your AI sends. Do not put anything private in a request you send to the Mesh.

6. What is recorded
The network owner's coordinator records which computer joined and when, job timings, results of jobs and a security log of events (joins, refusals, removals). Jobs and their answers are stored on the network owner's computer. Automatic deletion of old jobs is not built yet. Your computer keeps a small local log. No hidden monitoring or tamper-detection software is installed.

7. No reverse engineering
You agree not to reverse engineer, copy, extract or republish the Echo Mesh software or any private component of the network owner's AI that you may come into contact with. This is a contractual and licensing promise between us; it is not a technical guarantee.

8. Your computer, your risk
The software is provided as is, without warranty. It uses your computer's power, which may increase heat, fan noise and electricity use. You are responsible for your own computer and your own AI's behavior.

9. Changes
New abilities (new kinds of jobs, a higher limit) will not be switched on without asking you again.

10. Law
This agreement is between you and the network owner. [Governing law and contact details to be added by the network owner.]
"""


# ---- shown to EVERYONE first, before the Mesh configuration is chosen (the owner 2026-11-17). DRAFT in plain language, not legal advice:
# the publisher must have it reviewed before it goes to anyone outside the home. Bump TERMS_VERSION whenever the text changes. ----
TERMS_VERSION = "2026-11-17"
TERMS_READ = "I have read the terms and disclaimer above and I agree to them."
TERMS = """ECHO MESH: TERMS AND DISCLAIMER

1. What this is
Echo Mesh is software published by bruceatterberry-design (the \u201cPublisher\u201d). It lets computers share spare computing time with a private network, and lets AI programs on those computers exchange ideas. By installing or using it you agree to these terms.

2. No warranty
The software is provided \u201cAS IS\u201d and \u201cAS AVAILABLE\u201d, without warranty of any kind, express or implied, including any warranty of merchantability, fitness for a particular purpose, accuracy, reliability, security or non-infringement. It may contain errors and may stop working or change at any time.

3. The Publisher is not liable
To the fullest extent the law allows, bruceatterberry-design and the people who build or run the Mesh are not liable for any damage, loss or cost of any kind that comes from installing, running, or being unable to run the software. This includes lost or damaged data, hardware wear, heat, noise, electricity or internet costs, interrupted work, security incidents, and anything an AI says or does. Where the law does not allow a limit, the Publisher's total liability is limited to the amount you paid for the software, which is nothing.

4. Your computer, your responsibility
You decide to install this on your computer and you are responsible for it. Keep your own backups. You are responsible for the AI programs and models you connect and for obeying their licenses and terms.

5. AI can be wrong
AI answers can be incorrect, incomplete or unsuitable. Do not rely on them for legal, medical, financial, safety or other important decisions without checking them yourself.

6. Resources you control
The software uses processor time, memory, graphics power, electricity and network bandwidth. You set how much it may use, it gives way to your own work, and you can pause it or remove it at any time from the tray icon or by uninstalling it.

7. Privacy
Only the text of a job travels to the computer that runs it. The Mesh does not ask for your files, passwords or personal memories, and text that leaves the owner's computers has secrets and personal details removed first. The Mesh records which computers joined, job timings and a security log on the owner's computer.

8. Other people's software
The software can work with other programs, such as Ollama or an AI client you already use. Those belong to others and have their own terms. The Publisher is not responsible for them.

9. Ending and changes
Either side may end your use at any time, for any reason. The Publisher may change these terms; you will be asked again when they change in a way that matters.

10. Law and contact
[Governing law and contact details to be added by the Publisher.]
"""
