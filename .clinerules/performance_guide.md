# Performance Guide

## Standard Process Management

- In a Django Project, when efficienct computation is needed on CPU, the script should always employ a producer and consumer model. The ratio of which should be determined by the nature of the task. 
- In the main process, a script should spawn the same number of thread as the number of CPU threads available to retrieve information from database as producer.
- The script should by default spawn the same number of consumer processes as the number of CPU cores.
- The consumer process should never need Django context to start. They should take lean data type, and return lean data type.
- When converting data from and to Django objects overwhelms the main process, the script should skip the Django ORM and directly interact with database.
