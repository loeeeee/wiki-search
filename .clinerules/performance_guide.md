# Performance Guide

## Standard Process Management

- In a Django Project, when efficienct computation is needed on CPU, the script should always employ a producer and consumer model. The ratio of which should be determined by the nature of the task. 
- In the main process, a script should spawn certain number of thread to handle information retrieval from database as producer.
- In the main process, a script should also spawn a certain number of thread to handle saving data into database.
- The script should by default spawn the same number of consumer processes as the number of CPU cores.
- The consumer process should never need Django context to start. They should take lean data types, and return lean data types.
- When converting data from and to Django objects overwhelms the main process, the script should skip the Django ORM and directly interact with database.
- Always submit job as early as possible, and leave the blocking operations at the end of each iteration.
